"""Finite LINE drain keeps original decision, ordered candidates and native rack obligations."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.models import ReliableMessage
from tests.helpers import read_history_body


async def request_drain(flow, count=1):
    await flow.bins(count)
    flow.successor = "NONE"
    row = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", row["id"])
    return (await flow.all_rows("workline_state"))[0]


async def test_first_ready_fifo_requests_drain_without_existing_cycle(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow, 6)
    async with flow.factory() as session:
        msg = await session.get(ReliableMessage, state["drain_message_id"])
        assert json.loads(await read_history_body(session, (msg).request_record_id))["data"] == {
            "workline_code": "LINE-1",
            "required_slot_count": 6,
        }
    assert not await flow.all_rows("rack_cycle")
    await flow.advance("RETURN", (await flow.all_rows("bin_return"))[-1]["id"])
    assert (await flow.all_rows("workline_state"))[0] == state


async def test_scan4_prefers_original_supply_demand_before_drain(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await request_drain(flow)
    state = (await flow.all_rows("workline_state"))[0]
    assert state["drain_phase"] == "IDLE" and state["drain_message_id"] is None
    await flow.advance("FACE", demand)
    assert [r["purpose"] for r in await flow.all_rows("rack_cycle")] == ["ORDINARY"]


async def test_successor_becoming_none_rechecks_original_unassigned_fifo(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    row = (await flow.all_rows("bin_return"))[0]
    async with flow.factory() as session:
        upper = await flow.roller.get_pending_upper_id(session, kind="RETURN")
        pending = await flow.roller.list_pending(
            session, kind="RETURN", after_id=None, upper_id=upper, limit=20
        )
        assert RollerWorkRef("RETURN", row["id"]) in pending
    flow.successor = "NONE"
    await flow.advance("RETURN", row["id"])
    assert (await flow.all_rows("workline_state"))[0]["drain_phase"] == "REQUESTED"


@pytest.mark.parametrize("answer", ["WAIT", "READY"])
async def test_original_supply_request_survives_head_change_and_restart(roller_lifecycle, answer):
    from app.businesses._components.bin_roller.service import BinRoller

    flow = roller_lifecycle
    state = await request_drain(flow, 2)
    data = (
        dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000)
        if answer == "WAIT"
        else dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.decide_current(state["drain_message_id"], data)
    await flow.advance("DRAIN", state["line_run_id"])
    current = (await flow.all_rows("workline_state"))[0]
    old = flow.roller
    flow.roller = BinRoller(
        old.context,
        old.capabilities,
        old.production,
        rack_dispatch=old.rack_dispatch,
        tables=old.tables,
    )
    await flow.scan("SCAN3", "A000000003-B", timestamp=100)
    await flow.scan("SCAN3", "A000000001-B", timestamp=101)
    await flow.advance("DRAIN", state["line_run_id"])
    after = (await flow.all_rows("workline_state"))[0]
    for field in ("line_run_id", "drain_operation_id", "drain_message_id", "drain_retry_at"):
        assert after[field] == current[field]


async def test_expired_wait_registers_one_new_original_evaluation(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow)
    await flow.decide_current(
        state["drain_message_id"],
        dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000),
    )
    await flow.advance("DRAIN", state["line_run_id"])
    waiting = (await flow.all_rows("workline_state"))[0]
    await flow.advance("DRAIN", state["line_run_id"])
    assert (await flow.all_rows("workline_state"))[0] == waiting
    async with flow.factory.begin() as session:
        await flow.roller.repository(1).update(
            session, "workline_state", drain_retry_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    await flow.advance("DRAIN", state["line_run_id"])
    later = (await flow.all_rows("workline_state"))[0]
    assert (
        later["drain_operation_id"] != state["drain_operation_id"]
        and later["line_run_id"] == state["line_run_id"]
    )
    await flow.advance("DRAIN", state["line_run_id"])
    assert (await flow.all_rows("workline_state"))[0] == later


async def test_two_rack_drain_graph_uses_first_batch_before_new_supply(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow, 6)
    await flow.decide_current(
        state["drain_message_id"],
        dict(
            result="READY",
            racks=[dict(rack_id="D1", rack_face=["A", "B"]), dict(rack_id="D2", rack_face=["A"])],
        ),
    )
    await flow.advance("DRAIN", state["line_run_id"])
    first = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(first)
    await flow.advance("RACK_CYCLE", first["cycle_id"])
    await flow.complete_return(count=2)
    await flow.complete_return(no_batch=True)
    rotate = (await flow.all_rows("rack_action"))[-1]
    assert rotate["kind"] == "CTU02"
    await flow.rack_result(rotate)
    await flow.advance("RACK_CYCLE", first["cycle_id"])
    await flow.complete_return(count=2)
    await flow.complete_return(no_batch=True)
    await flow.finish_departure(first["cycle_id"])
    second = (await flow.all_rows("rack_action"))[-1]
    assert second["cycle_id"] != first["cycle_id"] and second["kind"] == "CTU01"
    await flow.rack_result(second)
    await flow.advance("RACK_CYCLE", second["cycle_id"])
    await flow.complete_return(count=2)
    await flow.finish_departure(second["cycle_id"])
    await flow.advance("DRAIN", state["line_run_id"])
    after = (await flow.all_rows("workline_state"))[0]
    assert after["drain_phase"] == "IDLE" and after["candidate_cursor"] is None
    assert not await flow.all_rows("feed")
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01", "CTU02", "CTU03", "CTU01", "CTU03"]


async def test_empty_buffer_keeps_registered_arrival_and_unused_candidates(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow)
    await flow.decide_current(
        state["drain_message_id"],
        dict(
            result="READY",
            racks=[dict(rack_id="D1", rack_face=["A", "B"]), dict(rack_id="D2", rack_face=["A"])],
        ),
    )
    await flow.advance("DRAIN", state["line_run_id"])
    entry = (await flow.all_rows("rack_action"))[0]
    # Simulate a definitive independent physical exit under the line transaction.
    async with flow.factory.begin() as session:
        row = (await flow.rows(session, "bin_return"))[0]
        await flow.roller.repository(1).close(session, "bin_return", row["id"], "EXITED")
    await flow.advance("DRAIN", state["line_run_id"])
    assert len(await flow.all_rows("rack_action")) == 1
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.finish_departure(entry["cycle_id"])
    await flow.advance("DRAIN", state["line_run_id"])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]
    assert (await flow.all_rows("workline_state"))[0]["drain_phase"] == "IDLE"


async def test_completed_supply_history_allows_new_demand(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow)
    async with flow.factory.begin() as session:
        row = (await flow.rows(session, "bin_return"))[0]
        await flow.roller.repository(1).close(session, "bin_return", row["id"], "EXITED")
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", state["line_run_id"])
    assert (await flow.all_rows("workline_state"))[0]["drain_phase"] == "IDLE"
    await flow.bins(1, offset=5)
    later = (await flow.all_rows("workline_state"))[0]
    assert later["drain_operation_id"] != state["drain_operation_id"]
    assert not await flow.all_rows("rack_action")


async def test_completed_or_removed_round_is_not_pending_scan_work(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    row = (await flow.all_rows("bin_return"))[0]
    async with flow.factory.begin() as session:
        await flow.roller.repository(1).close(session, "bin_return", row["id"], "EXITED")
        assert await flow.roller.get_pending_upper_id(session, kind="RETURN") is None


async def test_new_task_does_not_convert_original_drain_purpose(roller_lifecycle):
    flow = roller_lifecycle
    state = await request_drain(flow)
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", state["line_run_id"])
    entry = (await flow.all_rows("rack_action"))[0]
    flow.current_task, flow.successor = "T2", "UNKNOWN"
    demand = await flow.demand(task="T2")
    await flow.advance("FACE", demand)
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.origin.source_kind == "ROLLER_DRAIN" and job.origin.task_id is None
    assert any(
        f["purpose"] == "DRAIN" and f["scope_id"] == state["line_run_id"]
        for f in await flow.all_rows("face_work")
    )


@pytest.mark.parametrize(
    "case",
    [
        "zero",
        "bool",
        "extra",
        "empty_racks",
        "duplicate_racks",
        "duplicate_faces",
        "wait_reason",
        "wait_upper",
        "nullable",
    ],
)
async def test_drain_contract_is_strict(roller_lifecycle, case):
    from pydantic import ValidationError

    from app.businesses._components.bin_roller.request import (
        workline_return_buffer_drain_rack_decide_v1 as wire,
    )

    DECISION, DrainRequest = wire.DECISION, wire.DrainRequest

    invalid = {
        "zero": dict(workline_code="LINE-1", required_slot_count=0),
        "bool": dict(workline_code="LINE-1", required_slot_count=True),
        "extra": dict(workline_code="LINE-1", required_slot_count=2, task_id="T1"),
        "nullable": dict(workline_code=None, required_slot_count=1),
        "empty_racks": dict(result="READY", racks=[]),
        "duplicate_racks": dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A"])] * 2),
        "duplicate_faces": dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A", "A"])]),
        "wait_reason": dict(result="WAIT", reason_code="OTHER", retry_after_ms=1),
        "wait_upper": dict(
            result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60001
        ),
    }[case]
    with pytest.raises(ValidationError):
        (DECISION.validate_python if "result" in invalid else DrainRequest.model_validate)(invalid)
    valid = DECISION.validate_python(
        dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A", "B"])])
    )
    assert valid.racks[0].rack_face == ["A", "B"]
