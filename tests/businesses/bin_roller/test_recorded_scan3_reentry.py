"""Recorded SCAN3 reentry identities tested through current EVENT checkpoints."""

import json
from pathlib import Path

import pytest

from app.businesses._components.bin_roller.service import BinRoller
from app.models import ReliableMessage
from tests.helpers import read_history_body

BAD = json.loads((Path(__file__).parent / "fixtures/wes_recorded_cases.json").read_text())["bad"]
OTHER = "A000002701-B"


async def visit(flow, raw, time):
    await flow.scan("SCAN3", raw, timestamp=time)
    await flow.scan("SCAN4", raw, timestamp=time + 1)
    return next(
        r
        for r in reversed(await flow.current_rows("bin_return"))
        if r["bin_code"] == raw[:-2] and r["is_current"]
    )


async def ready(flow, row):
    await flow.result(row["scan4_command_code"], "SCAN4")
    await flow.advance_current("RETURN", row["id"])


async def candidates(flow):
    async with flow.factory() as session:
        return await flow.roller.get_return_candidates(session, work_line_id=1)


async def test_recorded_new_scan3_supersedes_old_unassociated_scan4_queue_shadow(roller_current):
    flow = roller_current
    raw = BAD["scan3_reentry"]["raw"]
    await flow.scan("SCAN4", raw, timestamp=1)
    orphan = (await flow.current_rows("bin_return"))[0]
    await flow.scan("SCAN3", raw, timestamp=10)
    other = await visit(flow, OTHER, 11)
    await ready(flow, other)
    await flow.scan("SCAN4", raw, timestamp=13)
    own = next(
        r
        for r in await flow.current_rows("bin_return")
        if r["bin_code"] == raw[:-2] and r["is_current"]
    )
    await ready(flow, own)
    assert [r["id"] for r in await candidates(flow)] == [other["id"], own["id"]]
    previous = next(r for r in await flow.current_rows("bin_return") if r["id"] == orphan["id"])
    assert previous["close_reason"] == "SUPERSEDED" and previous["scan4_command_code"] is None
    await flow.scan("SCAN4", raw, timestamp=1)
    assert (
        next(r for r in await flow.current_rows("bin_return") if r["id"] == orphan["id"])
        == previous
    )


async def test_recorded_scan3_reentry_rolls_back_and_does_not_separate_scan4(roller_current):
    flow = roller_current
    raw = BAD["scan3_reentry"]["raw"]
    original = await visit(flow, raw, 10)
    await flow.result(original["scan3_command_code"], "SCAN3")
    with pytest.raises(RuntimeError):
        async with flow.factory.begin() as session:
            await flow.scan("SCAN3", raw, timestamp=30, session=session)
            raise RuntimeError("rollback")
    assert len(await flow.current_rows("bin_return")) == 1
    await flow.scan("SCAN3", raw, timestamp=30)
    new = next(r for r in await flow.current_rows("bin_return") if r["is_current"])
    await flow.scan("SCAN4", raw, timestamp=31)
    assert (
        next(r for r in await flow.current_rows("bin_return") if r["id"] == new["id"])[
            "scan4_event_id"
        ]
        is None
    )
    await flow.result(original["scan4_command_code"], "SCAN4")
    await flow.scan("SCAN4", raw, timestamp=32)
    new = next(r for r in await flow.current_rows("bin_return") if r["id"] == new["id"])
    assert new["scan4_command_code"] not in (None, original["scan4_command_code"])
    await ready(flow, new)
    assert [r["id"] for r in await candidates(flow)] == [new["id"]]


@pytest.mark.parametrize("foreign_history", [False, True])
async def test_recorded_independent_scan3_requires_only_own_scan4_success(
    roller_current, foreign_history
):
    flow = roller_current
    raw = BAD["scan3_cancelled"]["raw"]
    flow.production.current_valid = False
    if foreign_history:
        flow.work_line_id = 2
        foreign = await visit(flow, raw, 1)
        flow.work_line_id = 1
    own = await visit(flow, raw, 10)
    if foreign_history:
        await flow.result(foreign["scan4_command_code"], "SCAN4")
        assert await candidates(flow) == ()
    await ready(flow, own)
    current = next(r for r in await flow.current_rows("bin_return") if r["id"] == own["id"])
    assert current["scan3_consumed_result_id"] is None
    assert [r["id"] for r in await candidates(flow)] == [own["id"]]


@pytest.mark.parametrize("old_status", ["SUCCESS", "FAILED"])
@pytest.mark.parametrize("old_result_before", [False, True])
async def test_recorded_reentry_preserves_old_facts_and_requires_current_fifo_success(
    roller_current, old_status, old_result_before
):
    flow = roller_current
    raw = BAD["scan3_reentry"]["raw"]
    old = await visit(flow, raw, 10)
    if old_result_before:
        await flow.result(old["scan3_command_code"], "SCAN3", status=old_status)
        await flow.result(old["scan4_command_code"], "SCAN4", status=old_status)
    other = await visit(flow, OTHER, 20)
    new = await visit(flow, raw, 30)
    assert new["id"] != old["id"]
    native = {}
    async with flow.factory() as session:
        for code in (old["scan3_command_code"], old["scan4_command_code"]):
            command = await flow.command(session, code)
            native[code] = (
                await read_history_body(session, (command).request_record_id),
                command.expires_at,
            )
    old_roller = flow.roller
    flow.roller = BinRoller(
        old_roller.context,
        old_roller.capabilities,
        old_roller.production,
        rack_dispatch=old_roller.rack_dispatch,
        tables=old_roller.tables,
    )
    for point in ("SCAN3", "SCAN4"):
        await flow.result(old[point.lower() + "_command_code"], point, status=old_status)
        await flow.scan(point, raw, timestamp=10 if point == "SCAN3" else 11)
    assert await candidates(flow) == ()
    await ready(flow, other)
    assert [r["id"] for r in await candidates(flow)] == [other["id"]]
    await ready(flow, new)
    assert [r["id"] for r in await candidates(flow)] == [other["id"], new["id"]]
    async with flow.factory() as session:
        for code, frozen in native.items():
            command = await flow.command(session, code)
            assert (
                await read_history_body(session, (command).request_record_id),
                command.expires_at,
            ) == frozen


@pytest.mark.parametrize("window", ["FACE_END", "PURE_RETURN", "SEGMENT"])
@pytest.mark.parametrize("allocated", [1, 2])
async def test_recorded_old_allocation_releases_unchanged_members_in_fifo(
    roller_return, window, allocated
):
    flow = roller_return
    raw = BAD["scan3_reentry"]["raw"]
    old = await visit(flow, raw, 10)
    other = await visit(flow, OTHER, 20)
    for row in (old, other):
        await ready(flow, row)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind=window)
        await flow.start(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
        message = await session.get(ReliableMessage, face["return_message_id"])
        original_raw = await read_history_body(session, (message).request_record_id)
    await visit(flow, raw, 30)
    async with flow.factory.begin() as session:
        await flow.decide(session, face_id, count=allocated)
        assert await flow.consume_return(session, face_id) == "SUPERSEDED_ROUNDS"
        current = await flow.roller.original_record(session, "face_work", face_id)
        assert current["current_transport_task_id"] is None and current["step_id"] is None
        assert current["phase"] == (
            "WAIT_FEED_DECISION" if window == "SEGMENT" else "RETURN_WINDOW"
        )
        assert (
            await read_history_body(
                session,
                (await session.get(ReliableMessage, face["return_message_id"])).request_record_id,
            )
            == original_raw
        )
    assert [r["id"] for r in await candidates(flow)] == [other["id"]]


@pytest.mark.parametrize("decision", ["READY", "NO_BATCH"])
async def test_recorded_allocation_tail_preserves_superseded_round(roller_return, decision):
    flow = roller_return
    raw = BAD["scan3_reentry"]["raw"]
    other, old = await visit(flow, OTHER, 10), await visit(flow, raw, 20)
    for row in (other, old):
        await ready(flow, row)
    await flow.result(old["scan3_command_code"], "SCAN3")
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
    new = await visit(flow, raw, 30)
    async with flow.factory.begin() as session:
        await flow.decide(
            session,
            face_id,
            count=1,
            data={"result": "NO_BATCH"} if decision == "NO_BATCH" else None,
        )
        await flow.consume_return(session, face_id)
    if decision == "READY":
        await flow.success(face_id)
        async with flow.factory.begin() as session:
            await flow.consume_return(session, face_id)
    await ready(flow, new)
    assert [r["id"] for r in await candidates(flow)] == (
        [other["id"], new["id"]] if decision == "NO_BATCH" else [new["id"]]
    )
    previous = next(r for r in await flow.current_rows("bin_return") if r["id"] == old["id"])
    assert not previous["is_current"] and previous["close_reason"] != "COMPLETED"
