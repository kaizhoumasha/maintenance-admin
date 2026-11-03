import asyncio

import pytest
from sqlalchemy import select

from app.businesses._components.bin_roller.ports import RollerWorkRef


async def test_two_scan4_wakeups_register_one_first_drain(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(2)
    scans = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"]
    if flow.factory.kw["bind"].dialect.name == "sqlite":
        for scan in scans:
            await flow.advance("SCAN", scan["id"])
    else:
        barrier = asyncio.Barrier(2)

        async def overlap(session, attempt):
            if attempt == 0:
                await session.execute(
                    select(flow.store.tables["decision"]).where(
                        flow.store.tables["decision"].c.kind == "DRAIN"
                    )
                )
                await asyncio.wait_for(barrier.wait(), 10)

        await asyncio.wait_for(
            asyncio.gather(*(flow.advance("SCAN", s["id"], before=overlap) for s in scans)), 20
        )
    drains = await flow.all_rows("drain")
    assert len(drains) == 1
    assert drains[0]["request"]["required_slot_count"] == 2


@pytest.mark.parametrize("decision", ["WAIT", "READY"])
async def test_old_empty_snapshot_restores_competing_consumed_decision(roller_lifecycle, decision):
    flow = roller_lifecycle
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("overlapping snapshots require PostgreSQL SERIALIZABLE")
    flow.successor = "NONE"
    await flow.bins(2)
    scans = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"]
    read_empty, changed = asyncio.Event(), asyncio.Event()
    attempts = []

    async def old_snapshot(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            assert not (
                await session.scalars(
                    select(flow.store.tables["decision"].c.id).where(
                        flow.store.tables["decision"].c.kind == "DRAIN"
                    )
                )
            ).all()
            await flow.roller.repository(1).return_queue(session)
            read_empty.set()
            await asyncio.wait_for(changed.wait(), 10)

    old = asyncio.create_task(flow.advance("SCAN", scans[0]["id"], before=old_snapshot))
    await asyncio.wait_for(read_empty.wait(), 10)
    await flow.advance("SCAN", scans[1]["id"])
    drain = (await flow.all_rows("drain"))[0]
    data = (
        dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000)
        if decision == "WAIT"
        else dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.decision("drain", drain["id"], data)
    await flow.advance("DRAIN", drain["id"])
    changed.set()
    await asyncio.wait_for(old, 20)
    assert len(attempts) >= 2
    assert [(d["id"], d["status"]) for d in await flow.all_rows("drain")] == [
        (drain["id"], decision)
    ]


async def test_different_work_kinds_share_original_face_transition(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    if flow.factory.kw["bind"].dialect.name == "sqlite":
        await flow.advance("FEED", demand)
        await flow.advance("RACK_CYCLE", cycle)
    else:
        barrier = asyncio.Barrier(2)

        async def overlap(session, attempt):
            if attempt == 0:
                await session.execute(
                    select(flow.store.tables["face_work"]).where(
                        flow.store.tables["face_work"].c.service_id.is_not(None)
                    )
                )
                await asyncio.wait_for(barrier.wait(), 10)

        await asyncio.wait_for(
            asyncio.gather(
                flow.advance("FEED", demand, before=overlap),
                flow.advance("RACK_CYCLE", cycle, before=overlap),
            ),
            20,
        )
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU02"]


async def test_roll_back_after_child_handle_keeps_original_demand_pending(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    demand = await flow.demand()
    transport = flow.roller.capabilities.transport
    original = transport.move_rack

    async def fail_after_handle(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("outer transaction failed after native handle")

    monkeypatch.setattr(transport, "move_rack", fail_after_handle)
    with pytest.raises(RuntimeError, match="outer transaction failed"):
        await flow.advance("FEED", demand)
    assert not await flow.all_rows("rack_action")
    assert not await flow.all_rows("rack_cycle")
    assert (await flow.all_rows("supply_demand"))[0]["cycle_id"] is None
    monkeypatch.setattr(transport, "move_rack", original)
    await flow.advance("FEED", demand)
    assert len(await flow.all_rows("rack_action")) == 1


async def test_restart_after_unknown_commit_restores_committed_identity(roller_lifecycle):
    from app.businesses._components.bin_roller.service import BinRoller

    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    async with flow.factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        await flow.roller.advance(session, RollerWorkRef("SCAN", scan["id"]))
        await session.commit()
        # The server committed; the caller lost its commit answer and all transient IDs.
    original = (await flow.all_rows("drain"))[0]
    old = flow.roller
    flow.roller = BinRoller(
        old.context,
        old.capabilities,
        old.production,
        rack_dispatch=old.rack_dispatch,
        tables=old.tables,
    )
    await flow.advance("SCAN", scan["id"])
    current = (await flow.all_rows("drain"))[0]
    assert len(await flow.all_rows("drain")) == 1
    assert (current["id"], current["operation_id"], current["message_id"]) == (
        original["id"],
        original["operation_id"],
        original["message_id"],
    )


async def test_empty_snapshot_during_original_commit_cannot_authorize_duplicate(roller_lifecycle):
    flow = roller_lifecycle
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("uncommitted concurrent visibility requires PostgreSQL")
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    observed_empty, original_committed = asyncio.Event(), asyncio.Event()
    attempts = []

    async def empty_snapshot(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            assert not (
                await session.scalars(
                    select(flow.store.tables["decision"].c.id).where(
                        flow.store.tables["decision"].c.kind == "DRAIN"
                    )
                )
            ).all()
            observed_empty.set()
            await asyncio.wait_for(original_committed.wait(), 10)

    async with flow.factory() as original:
        await original.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        await flow.roller.advance(original, RollerWorkRef("SCAN", scan["id"]))
        row = (await flow.rows(original, "drain"))[0]
        competitor = asyncio.create_task(flow.advance("SCAN", scan["id"], before=empty_snapshot))
        await asyncio.wait_for(observed_empty.wait(), 10)
        await original.commit()
        original_committed.set()
        await asyncio.wait_for(competitor, 20)
    assert len(attempts) >= 2
    assert [(d["id"], d["operation_id"]) for d in await flow.all_rows("drain")] == [
        (row["id"], row["operation_id"])
    ]


async def test_return_result_and_cycle_wakeup_prepare_one_next_feed(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    demand, cycle = await flow.ready_supply()
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    batch = (await flow.all_rows("return_batch"))[0]
    async with flow.factory.begin() as session:
        await flow.decide(session, batch["id"], count=1)
    await flow.advance("RETURN_BATCH", batch["id"])
    await flow.success(batch["id"])
    if flow.factory.kw["bind"].dialect.name == "sqlite":
        await flow.advance("RETURN_BATCH", batch["id"])
        await flow.advance("RACK_CYCLE", cycle)
    else:
        barrier = asyncio.Barrier(2)

        async def overlap(session, attempt):
            if attempt == 0:
                await session.execute(select(flow.store.tables["return_batch"]))
                await asyncio.wait_for(barrier.wait(), 10)

        await asyncio.wait_for(
            asyncio.gather(
                flow.advance("RETURN_BATCH", batch["id"], before=overlap),
                flow.advance("RACK_CYCLE", cycle, before=overlap),
            ),
            20,
        )
    assert [len(s["members"]) for s in await flow.all_rows("feed_segment")] == [4, 1]
    assert len(await flow.all_rows("return_batch")) == 1


async def test_original_result_consumption_and_next_outbox_roll_back_together(
    roller_lifecycle, monkeypatch
):
    from sqlalchemy import func

    from app.models import ReliableMessage

    flow = roller_lifecycle
    await flow.bins(1)
    demand, cycle = await flow.ready_supply()
    segment = (await flow.all_rows("feed_segment"))[0]
    await flow.feed_success(segment)
    wms = flow.roller.capabilities.wms
    original_send = wms.send
    async with flow.factory() as session:
        before = await session.scalar(select(func.count()).select_from(ReliableMessage))

    async def fail_after_outbox(*args, **kwargs):
        await original_send(*args, **kwargs)
        raise RuntimeError("outer rollback after WMS handle")

    monkeypatch.setattr(wms, "send", fail_after_outbox)
    with pytest.raises(RuntimeError, match="outer rollback"):
        await flow.advance("FEED", demand)
    saved = (await flow.all_rows("feed_segment"))[0]
    assert saved["id"] == segment["id"] and saved["consumed_revision"] is None
    assert not await flow.all_rows("return_opportunity")
    assert not await flow.all_rows("return_batch")
    async with flow.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == before
    monkeypatch.setattr(wms, "send", original_send)
    await flow.advance("FEED", demand)
    assert len(await flow.all_rows("return_batch")) == 1
    assert (await flow.all_rows("feed_segment"))[0]["transport_task_id"] == segment[
        "transport_task_id"
    ]


@pytest.mark.parametrize("failure", ["native_registration", "database"])
async def test_successor_registration_failure_still_rolls_back_original_release(
    roller_lifecycle, monkeypatch, failure
):
    from app.businesses._components.bin_roller import service as roller_service
    from app.core.errors import AppError, ErrorCode

    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", original)
    queued = await flow.demand(revision=2)
    await flow.advance("FEED", queued)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("RACK_CYCLE", cycle)
    action = (await flow.all_rows("rack_action"))[-1]
    await flow.accepted(action)
    transport = flow.roller.capabilities.transport
    original_move = transport.move_rack

    async def fail_after_handle(*args, **kwargs):
        await original_move(*args, **kwargs)
        raise RuntimeError("successor storage failure after native handle")

    async def database_failure(*args, **kwargs):
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "test database failure")

    with monkeypatch.context() as patch:
        if failure == "native_registration":
            patch.setattr(transport, "move_rack", fail_after_handle)
        else:
            patch.setattr(roller_service, "read_workline", database_failure)
        with pytest.raises(RuntimeError if failure == "native_registration" else AppError):
            await flow.advance("RACK_CYCLE", cycle)
    cycles = await flow.all_rows("rack_cycle")
    assert [(c["id"], c["status"]) for c in cycles] == [(cycle, "ACTIVE")]
    assert cycles[0]["release_basis"] is None
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]
    assert (
        next(d for d in await flow.all_rows("supply_demand") if d["id"] == queued)["cycle_id"]
        is None
    )
    await flow.advance("RACK_CYCLE", cycle)
    assert (
        next(c for c in await flow.all_rows("rack_cycle") if c["id"] == cycle)["status"]
        == "RELEASED"
    )
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03", "CTU01"]


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
@pytest.mark.parametrize("authority", ["FIFO", "DRAIN", "JOB"])
async def test_committed_scan_invalidates_older_dispatch_snapshot(roller_lifecycle, authority):
    from app.models import WorkLine

    flow = roller_lifecycle
    if authority == "JOB":
        demand, job = await flow.ready_supply(count=1)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        work = ("FEED", demand)
    else:
        flow.successor = "NONE"
        await flow.bins(1)
        fourth = next(r for r in await flow.all_rows("scan_arrival") if r["point"] == "SCAN4")
        work = ("SCAN", fourth["id"])
        if authority == "DRAIN":
            await flow.advance(*work)
            drain = (await flow.all_rows("drain"))[0]
            await flow.decision(
                "drain",
                drain["id"],
                dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])]),
            )
            work = ("DRAIN", drain["id"])
    old, accepted = asyncio.Event(), asyncio.Event()
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.scalar(select(WorkLine.id))
            old.set()
            await asyncio.wait_for(accepted.wait(), 5)

    dispatch = asyncio.create_task(flow.advance(*work, before=before))
    await asyncio.wait_for(old.wait(), 5)
    try:
        await flow.scan("SCAN3", "A000000033-B", timestamp=33)
        await flow.scan("SCAN3", "A000000033-B", timestamp=33)
    finally:
        accepted.set()
    await asyncio.wait_for(dispatch, 10)
    assert attempts == [0, 1]
    if authority == "FIFO":
        head = next(r for r in await flow.all_rows("scan_arrival") if r["point"] == "SCAN4")
        assert len(head["received_work"]) == 1
        assert len(await flow.all_rows("drain")) == 1
    elif authority == "DRAIN":
        assert len((await flow.all_rows("drain"))[0]["received_work"]) == 1
        assert len(await flow.all_rows("rack_action")) == 1
    else:
        async with flow.factory() as session:
            assert (
                await flow.roller.rack_dispatch.progress(session, job_id=job)
            ).facts_revision == 1
        assert not await flow.all_rows("departure")


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
async def test_new_scan_after_drain_creation_versions_the_committed_job(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(1)
    fourth = next(r for r in await flow.all_rows("scan_arrival") if r["point"] == "SCAN4")
    await flow.advance("SCAN", fourth["id"])
    drain = (await flow.all_rows("drain"))[0]
    await flow.decision(
        "drain", drain["id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    prepared, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = flow.roller.rack_dispatch.prepare

    async def pause(*args, **kwargs):
        action = await original(*args, **kwargs)
        prepared.set()
        await asyncio.wait_for(release.wait(), 5)
        return action

    monkeypatch.setattr(flow.roller.rack_dispatch, "prepare", pause)
    dispatch = asyncio.create_task(flow.advance("DRAIN", drain["id"]))
    await asyncio.wait_for(prepared.wait(), 5)
    lock = flow.lock_scan_line

    async def entering(session, line_id):
        started.set()
        return await lock(session, line_id)

    flow.lock_scan_line = entering
    receive = asyncio.create_task(flow.scan("SCAN3", "A000000034-B", timestamp=34))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert not receive.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(receive, dispatch), 10)
    [entry] = await flow.all_rows("rack_action")
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.facts_revision == 1 and job.current_action_id == entry["id"]
