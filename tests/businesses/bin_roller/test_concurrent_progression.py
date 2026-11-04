"""Current checkpoints keep original identities across concurrent transactions and rollback."""

import asyncio

import pytest
from sqlalchemy import func, select

from app.businesses._components.bin_roller.service import BinRoller
from app.models import ReliableMessage, TransportTask, WorkLine


async def overlap(flow, works):
    if flow.factory.kw["bind"].dialect.name == "sqlite":
        for kind, identity in works:
            await flow.advance(kind, identity)
        return
    gate = asyncio.Barrier(len(works))

    async def before(session, attempt):
        if attempt == 0:
            await session.scalar(select(WorkLine.id))
            await asyncio.wait_for(gate.wait(), 5)

    await asyncio.wait_for(
        asyncio.gather(*(flow.advance(kind, identity, before=before) for kind, identity in works)),
        15,
    )


async def test_two_scan4_wakeups_register_one_first_drain(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(2)
    flow.successor = "NONE"
    rows = await flow.all_rows("bin_return")
    await overlap(flow, [("RETURN", r["id"]) for r in rows])
    state = (await flow.all_rows("workline_state"))[0]
    async with flow.factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(ReliableMessage.id).where(
                            ReliableMessage.operation
                            == "workline.return_buffer.drain_rack_decide@v1"
                        )
                    )
                )
            )
            == 1
        )
    assert state["drain_phase"] == "REQUESTED"


async def test_different_work_kinds_share_original_face_transition(roller_lifecycle):
    flow = roller_lifecycle
    face_id, job_id = await flow.ready_supply(count=1)
    await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await overlap(flow, [("FACE", face_id), ("RACK_CYCLE", job_id)])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU02"]


async def test_roll_back_after_child_handle_keeps_original_demand_pending(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    face_id = await flow.demand()
    port = flow.roller.capabilities.transport
    original = port.move_rack

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("after native handle")

    monkeypatch.setattr(port, "move_rack", fail)
    with pytest.raises(RuntimeError, match="after native handle"):
        await flow.advance("FACE", face_id)
    assert not await flow.all_rows("rack_action") and not await flow.all_rows("rack_cycle")
    assert (await flow.all_rows("face_work"))[0]["rack_job_id"] is None
    monkeypatch.setattr(port, "move_rack", original)
    await flow.advance("FACE", face_id)
    assert len(await flow.all_rows("rack_action")) == 1


async def test_restart_after_unknown_commit_restores_committed_identity(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    flow.successor = "NONE"
    row = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", row["id"])
    before = (await flow.all_rows("workline_state"))[0]
    old = flow.roller
    flow.roller = BinRoller(
        old.context,
        old.capabilities,
        old.production,
        rack_dispatch=old.rack_dispatch,
        tables=old.tables,
    )
    await flow.advance("RETURN", row["id"])
    assert (await flow.all_rows("workline_state"))[0] == before


async def test_return_result_and_cycle_wakeup_prepare_one_next_feed(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    face_id, job_id = await flow.ready_supply()
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", face_id)
    async with flow.factory.begin() as session:
        await flow.decide(session, face_id, count=1)
    await flow.advance("FACE", face_id)
    await flow.success(face_id)
    await overlap(flow, [("FACE", face_id), ("RACK_CYCLE", job_id)])
    row = next(r for r in await flow.all_rows("face_work") if r["id"] == face_id)
    assert row["feed_cursor"] == 4 and row["phase"] == "FEED_MOVE"
    async with flow.factory() as session:
        moves = list(
            await session.scalars(
                select(TransportTask).where(
                    TransportTask.cycle_id == job_id, TransportTask.kind == "BIN_MOVE"
                )
            )
        )
        assert sorted(len(m.frozen["moves"]) for m in moves) == [1, 1, 4]


async def test_original_result_consumption_and_next_outbox_roll_back_together(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    await flow.bins(1)
    face_id, job_id = await flow.ready_supply()
    before = next(r for r in await flow.all_rows("face_work") if r["id"] == face_id)
    await flow.complete_transport(before["current_transport_task_id"])
    port = flow.roller.capabilities.wms
    send = port.send
    async with flow.factory() as session:
        count = await session.scalar(select(func.count()).select_from(ReliableMessage))

    async def fail(*args, **kwargs):
        await send(*args, **kwargs)
        raise RuntimeError("after WMS handle")

    monkeypatch.setattr(port, "send", fail)
    with pytest.raises(RuntimeError, match="after WMS handle"):
        await flow.advance("FACE", face_id)
    assert next(r for r in await flow.all_rows("face_work") if r["id"] == face_id) == before
    async with flow.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == count
    monkeypatch.setattr(port, "send", send)
    await flow.advance("FACE", face_id)
    assert (
        next(r for r in await flow.all_rows("face_work") if r["id"] == face_id)["phase"]
        == "WAIT_RETURN_DECISION"
    )


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
@pytest.mark.parametrize("answer", ["WAIT", "READY"])
async def test_old_empty_snapshot_restores_competing_consumed_decision(roller_lifecycle, answer):
    flow = roller_lifecycle
    await flow.bins(2)
    flow.successor = "NONE"
    rows = await flow.all_rows("bin_return")
    old_read, changed = asyncio.Event(), asyncio.Event()
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(flow.store.tables["workline_state"]))
            old_read.set()
            await asyncio.wait_for(changed.wait(), 5)

    old = asyncio.create_task(flow.advance("RETURN", rows[0]["id"], before=before))
    await asyncio.wait_for(old_read.wait(), 5)
    try:
        await flow.advance("RETURN", rows[1]["id"])
        state = (await flow.all_rows("workline_state"))[0]
        data = (
            dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000)
            if answer == "WAIT"
            else dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
        )
        await flow.decide_current(state["drain_message_id"], data)
        await flow.advance("DRAIN", state["line_run_id"])
    finally:
        changed.set()
    await asyncio.wait_for(old, 15)
    assert attempts == [0, 1]
    after = (await flow.all_rows("workline_state"))[0]
    assert after["drain_message_id"] == state["drain_message_id"] and after["drain_phase"] == (
        "WAIT" if answer == "WAIT" else "USING"
    )


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
async def test_new_scan_after_drain_creation_versions_the_committed_job(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    await flow.bins(1)
    flow.successor = "NONE"
    row = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", row["id"])
    state = (await flow.all_rows("workline_state"))[0]
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    prepared, release, entering = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = flow.roller.rack_dispatch.prepare

    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        prepared.set()
        await asyncio.wait_for(release.wait(), 5)
        return result

    monkeypatch.setattr(flow.roller.rack_dispatch, "prepare", pause)
    dispatch = asyncio.create_task(flow.advance("DRAIN", state["line_run_id"]))
    await asyncio.wait_for(prepared.wait(), 5)
    lock = flow.lock_scan_line

    async def receive_lock(session, line):
        entering.set()
        return await lock(session, line)

    flow.lock_scan_line = receive_lock
    receive = asyncio.create_task(flow.scan("SCAN3", "A000000034-B", timestamp=34))
    try:
        await asyncio.wait_for(entering.wait(), 5)
        assert not receive.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(receive, dispatch), 15)
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.facts_revision == 1 and job.current_action_id == entry["id"]
