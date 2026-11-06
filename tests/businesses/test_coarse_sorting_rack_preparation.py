"""Shared rack preparation through original receivers and transactional ports."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.businesses.coarse_sorting import composition
from app.businesses.coarse_sorting.models import RackPreparation
from app.tasks.reliable_tasks import run_cleanup
from tests.helpers import (
    allocated,
    deliver_transport_callback,
    enable_racks,
    reach_allocate,
    reach_move,
)


async def test_two_reels_share_one_active_preparation(rack):
    flow = rack
    flow.peer.allocate = {
        "result": "REPLENISH_REQUIRED",
        "workline_code": flow.workline,
        "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
    }
    first = await reach_allocate(flow)
    second = await reach_allocate(flow, timestamp=1793206799001)
    enable_racks(flow)
    if flow.factory.kw["bind"].dialect.name == "postgresql":
        await asyncio.gather(*(flow.service.advance(row["id"]) for row in (first, second)))
    else:
        for row in (first, second):
            await flow.service.advance(row["id"])
    rows = await flow.rows()
    assert {row["state"] for row in rows} == {"AWAITING_RACK"}
    assert len({row["rack_preparation_id"] for row in rows}) == 1


async def test_replenish_requires_original_applied_arrival(rack):
    flow = rack
    flow.peer.allocate = {
        "result": "REPLENISH_REQUIRED",
        "workline_code": flow.workline,
        "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
    }
    row = await reach_allocate(flow)
    enable_racks(flow)
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"
    prep = (await flow.rows())[0]["rack_preparation_id"]
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    assert await flow.dispatch() == 1
    wire = flow.peer.transports[-1]
    assert wire["data"]["rcs_template_id"] == "F01"
    assert wire["data"]["target_face"] == "90"
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["data"]["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["data"]["target"],
            "arrival_face": "90",
        },
    )
    assert await flow.service.advance_preparation(prep) == "READY"
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    actions = [a for a in await flow.action_rows(row["id"]) if a["kind"] == "ALLOCATE_STORAGE"]
    assert len(actions) == 2
    assert actions[1]["operation_id"] != actions[0]["operation_id"]
    assert actions[1]["input_preparation_id"] == prep


async def prepare(flow, mode="REPLENISH_REQUIRED"):
    flow.peer.allocate = {
        "result": mode,
        "workline_code": flow.workline,
        "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
        **({"current_rack": {"rack_id": "OLD"}} if mode == "REPLACE_REQUIRED" else {}),
    }
    row = await reach_allocate(flow)
    enable_racks(flow)
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"
    return row, (await flow.rows())[0]["rack_preparation_id"]


@pytest.mark.parametrize("face", [None, "270"])
async def test_inferred_or_wrong_direction_never_releases_waiting_reel(rack, face):
    flow = rack
    row, prep = await prepare(flow)
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            **({"arrival_face": face} if face else {}),
        },
    )
    assert await flow.service.advance_preparation(prep) != "READY"
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"
    assert len(flow.peer.transports) == 1


async def test_failed_original_entry_stays_pending_without_replacement(rack):
    flow = rack
    row, prep = await prepare(flow)
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "FAILED",
            "position_unknown": True,
            "failure_code": "POSITION_UNKNOWN",
        },
    )
    for _ in range(2):
        assert await flow.service.advance_preparation(prep) == "RACK_RECONCILIATION_REQUIRED"
    assert await flow.dispatch() == 0
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"


async def test_replace_adopts_wms_presence_and_enters_after_original_departure_acceptance(rack):
    flow = rack
    row, prep = await prepare(flow, "REPLACE_REQUIRED")
    flow.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "STORE"},
    }
    assert await flow.service.advance_preparation(prep) == "AWAITING_DEPARTURE_RESULT"
    assert await flow.dispatch() == 1
    import json

    departure = json.loads(flow.peer.requests[-1])
    assert departure["operation"] == "rack.departure_decide@v1"
    assert departure["data"]["current_face"] is None
    assert not flow.peer.transports
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_ACCEPTANCE"
    assert await flow.dispatch() == 1
    assert flow.peer.transports[-1]["data"]["rack_id"] == "OLD"
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    assert await flow.dispatch() == 1
    assert [w["data"]["rack_id"] for w in flow.peer.transports] == ["OLD", "NEW"]
    assert all(w["data"]["rcs_template_id"] == "F01" for w in flow.peer.transports)
    assert await flow.service.advance(row["id"]) == "AWAITING_RACK"


async def test_pending_sweep_advances_waiting_rack_independently(rack):
    flow = rack
    _, prep = await prepare(flow)
    await flow.service.scan_pending(limit=1)
    assert await flow.dispatch() == 1
    assert flow.peer.transports[-1]["data"]["rack_id"] == "NEW"


async def second_allocation(flow, decision):
    flow.peer.allocate = decision
    row = await reach_move(flow, timestamp=1793206799001)
    await flow.dispatch()
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    await flow.dispatch()
    return row


@pytest.mark.parametrize("mode", ["REPLACE_REQUIRED", "DEPART_REQUIRED"])
async def test_new_put_and_departure_cannot_both_get_conflicting_authority(rack, mode):
    flow = rack
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    second = await second_allocation(
        flow,
        {
            "result": mode,
            "workline_code": flow.workline,
            "current_rack": {"rack_id": "RACK-1"},
            **(
                {"incoming_rack": {"rack_id": "NEW", "target_face": "90"}}
                if mode == "REPLACE_REQUIRED"
                else {}
            ),
        },
    )
    enable_racks(flow)
    if flow.factory.kw["bind"].dialect.name == "postgresql":
        await asyncio.gather(flow.service.advance(first["id"]), flow.service.advance(second["id"]))
    else:
        await flow.service.advance(first["id"])
        await flow.service.advance(second["id"])
    rows = {r["id"]: r for r in await flow.rows()}
    prep = rows[second["id"]]["rack_preparation_id"]
    reason = await flow.service.advance_preparation(prep)
    if rows[first["id"]]["state"] == "AWAITING_PUT":
        assert reason == "AWAITING_PREDECESSORS"
        assert not flow.peer.transports
        put = next(a for a in await flow.action_rows(first["id"]) if a["kind"] == "PUT_TO_BIN")
        await flow.dispatch()
        assert (
            await flow.result(
                first,
                command_code=put["command_code"],
                device_code=flow.output,
                data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
            )
        ).status_code == 200
        assert await flow.service.advance_preparation(prep) == "AWAITING_DEPARTURE_RESULT"
        # Physical placement closes its direct responsibility before the independent report ACK.
        assert not flow.peer.reports
        assert (await flow.rows())[0]["state"] == "AWAITING_PUT"
    else:
        assert rows[first["id"]]["state"] == "AWAITING_RACK"
        assert not any(a["kind"] == "PUT_TO_BIN" for a in await flow.action_rows(first["id"]))


async def test_arrival_does_not_allow_wms_to_reuse_the_old_rack(rack):
    flow = rack
    row, prep = await prepare(flow)
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "90",
        },
    )
    assert await flow.service.advance_preparation(prep) == "READY"
    flow.peer.allocate = allocated(flow, rack_id="OLD")
    await flow.service.advance(row["id"])
    await flow.dispatch()
    assert await flow.service.advance(row["id"]) == "RACK_STATE_CONFLICT"
    assert not any(a["kind"] == "PUT_TO_BIN" for a in await flow.action_rows(row["id"]))


async def test_departure_wait_uses_one_original_request_until_its_due_time(rack):
    flow = rack
    _, prep = await prepare(flow, "REPLACE_REQUIRED")
    flow.peer.departure = {"result": "WAIT", "retry_after_ms": 5000}
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    await flow.service.advance_preparation(prep)
    assert await flow.dispatch() == 0
    flow.now += timedelta(milliseconds=4999)
    await flow.service.advance_preparation(prep)
    assert await flow.dispatch() == 0
    flow.now += timedelta(milliseconds=1)
    await flow.service.advance_preparation(prep)
    assert await flow.dispatch() == 1
    decisions = [
        json.loads(w)
        for w in flow.peer.requests
        if json.loads(w)["operation"] == "rack.departure_decide@v1"
    ]
    assert len(decisions) == 2
    assert decisions[0]["operation_id"] != decisions[1]["operation_id"]
    assert decisions[0]["data"] == decisions[1]["data"]


async def test_failed_registration_rolls_back_preparation_reference_and_outbox(rack, monkeypatch):
    flow = rack
    _, prep = await prepare(flow)
    dispatch = flow.service.rack_dispatch
    original = dispatch.prepare

    async def fail_after_registration(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("test rollback")

    monkeypatch.setattr(dispatch, "prepare", fail_after_registration)
    with pytest.raises(RuntimeError, match="test rollback"):
        await flow.service.advance_preparation(prep)
    assert await flow.dispatch() == 0
    monkeypatch.setattr(dispatch, "prepare", original)
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    assert await flow.dispatch() == 1
    assert len(flow.peer.transports) == 1


async def test_applied_original_correction_resumes_same_entry_via_public_readers(rack, monkeypatch):
    flow = rack
    _, prep = await prepare(flow)
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "FAILED",
            "position_unknown": True,
            "failure_code": "POSITION_UNKNOWN",
        },
    )
    assert await flow.service.advance_preparation(prep) == "RACK_RECONCILIATION_REQUIRED"
    port = flow.service.capabilities.transport
    read, predecessors = port.get_result, port.list_unclosed_predecessors
    identities = []

    async def corrected(session, **identity):
        result = await read(session, **identity)
        identities.append(result.transport_task_id)
        assert result.transport_task_id == wire["transport_task_id"]
        return replace(
            result,
            outcome={
                **result.outcome,
                "outcome_revision": 2,
                "status": "SUCCEEDED",
                "position_unknown": None,
                "failure_code": None,
                "final_position": wire["target"],
                "arrival_face": "90",
            },
            outcome_revision=2,
            arrival_face_reported=True,
            closure="CLOSED",
            closure_reason="APPLIED_RESULT",
        )

    async def corrected_predecessors(session, **scope):
        result = await predecessors(session, **scope)
        return replace(
            result,
            items=tuple(
                replace(item, closure="CLOSED", task_closure="CLOSED", outcome_revision=2)
                for item in result.items
            ),
        )

    # The independently scoped Transport correction is represented only at its public boundary.
    monkeypatch.setattr(port, "get_result", corrected)
    monkeypatch.setattr(port, "list_unclosed_predecessors", corrected_predecessors)
    assert await flow.service.advance_preparation(prep) == "READY"
    assert set(identities) == {wire["transport_task_id"]}
    assert await flow.dispatch() == 0
    assert len(flow.peer.transports) == 1


async def test_another_workline_progresses_while_a_point_waits_for_arrival(rack):
    flow = rack
    _, prep = await prepare(flow)
    assert (await flow.post(flow.event(device=1))).status_code == 200
    async with flow.factory() as session:
        other = (
            (
                await session.execute(
                    select(flow.table).where(flow.table.c.work_line_id == flow.line_ids[1])
                )
            )
            .mappings()
            .one()
        )
    try:
        assert await flow.service.advance(other["id"]) == "AWAITING_VALIDATION_RESULT"
        await flow.dispatch()
        assert await flow.service.advance(other["id"]) == "AWAITING_MOVE_RESULT"
        async with flow.factory() as session:
            current = await session.get(RackPreparation, prep)
            assert current.state == "MOVING_IN" and current.current_action_id is None
    finally:
        async with flow.factory.begin() as session:
            await session.execute(
                flow.table.update()
                .where(flow.table.c.id == other["id"])
                .values(current_action_id=None)
            )
            for identity in await session.scalars(
                select(flow.actions.c.id)
                .where(flow.actions.c.job_id == other["id"])
                .order_by(flow.actions.c.created_at.desc(), flow.actions.c.id.desc())
            ):
                await session.execute(delete(flow.actions).where(flow.actions.c.id == identity))
            await session.execute(delete(flow.table).where(flow.table.c.id == other["id"]))


async def test_late_allocated_decision_cannot_create_put_after_preparation_started(rack):
    flow = rack
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    second = await second_allocation(
        flow,
        {
            "result": "REPLENISH_REQUIRED",
            "workline_code": flow.workline,
            "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
        },
    )
    enable_racks(flow)
    assert await flow.service.advance(second["id"]) == "AWAITING_RACK"
    assert await flow.service.advance(first["id"]) == "AWAITING_RACK"
    rows = await flow.rows()
    assert len({row["rack_preparation_id"] for row in rows}) == 1
    original = next(
        a for a in await flow.action_rows(first["id"]) if a["kind"] == "ALLOCATE_STORAGE"
    )
    assert original["evidence_history"][-1]["data"]["result"] == "ALLOCATED"
    assert original["result_preparation_id"] == rows[0]["rack_preparation_id"]
    assert not any(a["kind"] == "PUT_TO_BIN" for a in await flow.action_rows(first["id"]))


async def test_default_formal_assembly_cannot_advance_candidate_preparation(rack):
    flow = rack
    _, prep = await prepare(flow)
    formal = composition.build_service(flow.service.capabilities)
    assert await formal.scan_pending() == 0
    assert await formal.advance_preparation(prep) == "RACK_PREPARATION_STAGE_REQUIRED"
    assert await flow.dispatch() == 0


@pytest.mark.parametrize("exhaust", [False, True])
async def test_real_serialization_conflicts_retry_preparation_in_fresh_sessions(
    rack, monkeypatch, exhaust
):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    flow = rack
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL serialization contract")
    _, prep = await prepare(flow)
    execute = AsyncSession.execute
    sessions = []
    table = RackPreparation.__table__

    async def collide(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if (
            getattr(statement, "is_select", False)
            and getattr(statement, "_for_update_arg", None) is None
            and table in statement.get_final_froms()
            and (await session.connection())
            .sync_connection.get_execution_options()
            .get("isolation_level")
            == "SERIALIZABLE"
            and session not in sessions
        ):
            sessions.append(session)
            if len(sessions) == 1 or exhaust:
                async with flow.factory.begin() as writer:
                    await writer.execute(
                        table.update()
                        .where(table.c.id == prep)
                        .values(wait_reason=f"EXTERNAL_WRITE_{len(sessions)}")
                    )
        return result

    monkeypatch.setattr(AsyncSession, "execute", collide)
    if exhaust:
        with pytest.raises(DBAPIError) as error:
            await flow.service.advance_preparation(prep)
        assert error.value.orig.sqlstate == "40001"
    else:
        assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    assert len(sessions) == (3 if exhaust else 2)
    assert len({id(session) for session in sessions}) == len(sessions)
    assert all(not session.in_transaction() for session in sessions)
    assert await flow.dispatch() == (0 if exhaust else 1)


async def test_missing_original_predecessor_cannot_be_treated_as_clear(rack):
    flow = rack
    _, prep = await prepare(flow)
    async with flow.factory.begin() as session:
        await session.execute(
            RackPreparation.__table__.update()
            .where(RackPreparation.id == prep)
            .values(predecessor_action_ids=["missing-original-action"])
        )
    assert await flow.service.advance_preparation(prep) == "AWAITING_PREDECESSORS"
    assert await flow.dispatch() == 0


async def test_lost_current_entry_reference_is_not_reconstructed(rack):
    flow = rack
    _, prep = await prepare(flow)
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    async with flow.factory.begin() as session:
        await session.execute(
            RackPreparation.__table__.update()
            .where(RackPreparation.id == prep)
            .values(current_action_id=None)
        )
    assert await flow.service.advance_preparation(prep) == "PREPARATION_RECONCILIATION_REQUIRED"


async def test_fresh_reel_joins_active_preparation_before_first_allocation(rack):
    flow = rack
    _, prep = await prepare(flow)
    fresh = await reach_move(flow, timestamp=1793206799001)
    await flow.dispatch()
    assert (await flow.result(fresh)).status_code == 200
    assert await flow.service.advance(fresh["id"]) == "AWAITING_RACK"
    assert await flow.service.advance(fresh["id"]) == "AWAITING_RACK"
    waiting = (await flow.rows())[-1]
    assert waiting["rack_position_code"] is not None
    assert waiting["rack_binding_version"] is not None
    assert not any(a["kind"] == "ALLOCATE_STORAGE" for a in await flow.action_rows(fresh["id"]))
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "90",
        },
    )
    assert await flow.service.advance_preparation(prep) == "READY"
    assert await flow.service.advance(fresh["id"]) == "AWAITING_ALLOCATION_RESULT"
    allocation = next(
        a for a in await flow.action_rows(fresh["id"]) if a["kind"] == "ALLOCATE_STORAGE"
    )
    assert allocation["round_no"] == 1 and allocation["input_preparation_id"] == prep
    assert allocation["request_data"]["binding_id"] == fresh["binding_id"]


async def test_pending_original_rack_evidence_survives_actual_terminal_cleanup(rack):
    from app.models import ReliableMessage

    flow = rack
    row, prep = await prepare(flow, "REPLACE_REQUIRED")
    source = next(a for a in await flow.action_rows(row["id"]) if a["kind"] == "ALLOCATE_STORAGE")
    old = flow.now - timedelta(days=100)
    async with flow.factory.begin() as session:
        unrelated = ReliableMessage(
            target="coarse-wms",
            operation="other.owner.fact",
            operation_id="unrelated-retention",
            scope_owner="another_owner",
            scope_kind="TASK",
            scope_id="other-task",
            request_raw=b"{}",
            status="DONE",
            attempts=0,
            next_attempt_at=old,
            created_at=old,
            updated_at=old,
        )
        session.add(unrelated)
        await session.flush()
        unrelated_id = unrelated.id

    async def clean_original(message_id):
        async with flow.factory.begin() as session:
            await session.execute(
                ReliableMessage.__table__.update()
                .where(ReliableMessage.id == message_id)
                .values(updated_at=old)
            )
        await run_cleanup(flow.factory, 90, now=flow.now, batch=1)
        async with flow.factory() as session:
            assert await session.get(ReliableMessage, message_id) is not None
            assert await session.get(ReliableMessage, unrelated_id) is None

    await clean_original(source["message_id"])
    flow.peer.departure = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "STORE"},
    }
    assert await flow.service.advance_preparation(prep) == "AWAITING_DEPARTURE_RESULT"
    await flow.dispatch()
    async with flow.factory() as session:
        public = (
            await flow.service.rack_dispatch.jobs(
                session, work_line_id=flow.line_ids[0], source_kinds=("COARSE_WMS_PRESENT",)
            )
        )[0]
    decision = public.actions[-1]
    await clean_original(decision.message_id)
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_ACCEPTANCE"
    await flow.dispatch()
    async with flow.factory() as session:
        public = await flow.service.rack_dispatch.progress(session, job_id=public.id)
    departure = public.actions[-1]
    await clean_original(departure.submit_message_id)
    assert await flow.service.advance_preparation(prep) == "AWAITING_RACK_RESULT"
    assert flow.peer.transports[-1]["data"]["transport_task_id"] == departure.transport_task_id
    assert [a.decision_operation_id for a in public.actions if a.kind == "DEPARTURE_DECIDE"] == [
        decision.decision_operation_id
    ]


@pytest.mark.parametrize("proof", ["complete", "missing", "unknown", "wrong_call", "wrong_task"])
async def test_original_saved_put_completion_is_required_when_command_was_removed(rack, proof):
    from app.models import EcsCommand, EcsResultEvidence

    flow = rack
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    second = await second_allocation(
        flow,
        {
            "result": "REPLACE_REQUIRED",
            "workline_code": flow.workline,
            "current_rack": {"rack_id": "RACK-1"},
            "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
        },
    )
    enable_racks(flow)
    assert await flow.service.advance(first["id"]) == "AWAITING_PUT_RESULT"
    assert await flow.service.advance(second["id"]) == "AWAITING_RACK"
    prep = (await flow.rows())[-1]["rack_preparation_id"]
    put = next(a for a in await flow.action_rows(first["id"]) if a["kind"] == "PUT_TO_BIN")
    await flow.dispatch()
    if proof != "unknown":
        assert (
            await flow.result(
                first,
                command_code=put["command_code"],
                device_code=flow.output,
                data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
            )
        ).status_code == 200
        assert await flow.service.advance(first["id"]) == "AWAITING_REPORT_RESULT"
    async with flow.factory.begin() as session:
        await session.execute(
            delete(EcsResultEvidence).where(EcsResultEvidence.command_code == put["command_code"])
        )
        await session.execute(
            delete(EcsCommand).where(EcsCommand.command_code == put["command_code"])
        )
        if proof == "missing":
            await session.execute(
                flow.actions.update()
                .where(flow.actions.c.id == put["id"])
                .values(evidence_history=[])
            )
        elif proof in ("wrong_call", "wrong_task"):
            action = (
                (await session.execute(select(flow.actions).where(flow.actions.c.id == put["id"])))
                .mappings()
                .one()
            )
            evidence = [dict(item) for item in action["evidence_history"]]
            for item in evidence:
                if "applied_put" in item:
                    item["applied_put"] = {
                        **item["applied_put"],
                        "call_key" if proof == "wrong_call" else "task_type": "different-original",
                    }
            await session.execute(
                flow.actions.update()
                .where(flow.actions.c.id == put["id"])
                .values(evidence_history=evidence)
            )
    expected = "AWAITING_DEPARTURE_RESULT" if proof == "complete" else "AWAITING_PREDECESSORS"
    assert await flow.service.advance_preparation(prep) == expected
    assert not flow.peer.transports


@pytest.mark.parametrize(
    "field,value",
    [("task_type", "MOVE_FORWARD"), ("device_code", "OTHER"), ("command_code", "OTHER")],
)
async def test_live_put_predecessor_requires_exact_original_command(
    rack, monkeypatch, field, value
):
    flow = rack
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    second = await second_allocation(
        flow,
        {
            "result": "REPLACE_REQUIRED",
            "workline_code": flow.workline,
            "current_rack": {"rack_id": "RACK-1"},
            "incoming_rack": {"rack_id": "NEW", "target_face": "90"},
        },
    )
    enable_racks(flow)
    await flow.service.advance(first["id"])
    await flow.service.advance(second["id"])
    prep = (await flow.rows())[-1]["rack_preparation_id"]
    put = next(a for a in await flow.action_rows(first["id"]) if a["kind"] == "PUT_TO_BIN")
    await flow.dispatch()
    assert (
        await flow.result(
            first,
            command_code=put["command_code"],
            device_code=flow.output,
            data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
        )
    ).status_code == 200
    read = flow.service.capabilities.ecs.get_command

    async def mismatched(session, **identity):
        command = await read(session, **identity)
        return (
            replace(command, **{field: value})
            if identity["command_code"] == put["command_code"]
            else command
        )

    monkeypatch.setattr(flow.service.capabilities.ecs, "get_command", mismatched)
    assert await flow.service.advance_preparation(prep) == "AWAITING_PREDECESSORS"
    assert not flow.peer.transports


async def test_unrelated_first_allocation_after_ready_does_not_inherit_old_preparation(rack):
    flow = rack
    _, prep = await prepare(flow)
    await flow.service.advance_preparation(prep)
    await flow.dispatch()
    wire = flow.peer.transports[-1]["data"]
    await deliver_transport_callback(
        flow.factory,
        "transport.task.resulted@v1",
        {
            "transport_task_id": wire["transport_task_id"],
            "kind": "RACK_MOVE",
            "outcome_revision": 1,
            "rack_id": "NEW",
            "status": "SUCCEEDED",
            "final_position": wire["target"],
            "arrival_face": "90",
        },
    )
    assert await flow.service.advance_preparation(prep) == "READY"
    fresh = await reach_move(flow, timestamp=1793206799001)
    await flow.dispatch()
    assert (await flow.result(fresh)).status_code == 200
    assert await flow.service.advance(fresh["id"]) == "AWAITING_ALLOCATION_RESULT"
    allocation = next(
        a for a in await flow.action_rows(fresh["id"]) if a["kind"] == "ALLOCATE_STORAGE"
    )
    assert allocation["round_no"] == 1 and allocation["input_preparation_id"] is None
    assert allocation["rack_generation_no"] == 1
