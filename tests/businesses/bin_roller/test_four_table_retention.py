from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.models import CallbackReceipt, ReliableMessage
from app.reliable import ScopeRef


async def test_active_canonical_and_consumed_forward_anchor_survive_cleanup(
    roller_current, monkeypatch
):
    from app.businesses._components.bin_roller import retention
    from app.tasks.callback_tasks import run_receipt_cleanup
    from app.tasks.reliable_tasks import run_cleanup

    flow = roller_current
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    original = (await flow.current_rows("bin_passage"))[0]
    await flow.result(original["scan1_command_code"], "SCAN1")
    await flow.advance_current("PASSAGE", original["id"])
    cutoff = datetime.now(UTC) - timedelta(days=100)
    async with flow.factory.begin() as session:
        await session.execute(update(CallbackReceipt).values(created_at=cutoff))
        await session.execute(update(ReliableMessage).values(updated_at=cutoff))
    await run_receipt_cleanup(flow.factory, 90, batch=1)
    await run_cleanup(flow.factory, 90, batch=1)
    async with flow.factory() as session:
        assert await session.get(CallbackReceipt, original["scan1_event_id"])
        command = await flow.ecs.get_command(
            session, owner=flow.roller.context, command_code=original["scan1_command_code"]
        )
        assert command.message_id is not None
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    assert len(await flow.current_rows("bin_passage")) == 1


async def test_protected_first_page_does_not_starve_later_expired_rows(roller_current, monkeypatch):
    from app.businesses._components.bin_roller import retention
    from app.tasks.callback_tasks import run_receipt_cleanup

    flow = roller_current
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    anchor = (await flow.current_rows("bin_passage"))[0]
    async with flow.factory.begin() as session:
        for number in range(5):
            session.add(
                CallbackReceipt(
                    operation="unrelated",
                    operation_id=str(number),
                    raw_digest="a" * 64,
                    compare_mode="RAW",
                    status_code=200,
                    body={},
                    resp_timestamp=1,
                    created_at=datetime.now(UTC) - timedelta(days=100),
                )
            )
        await session.execute(
            update(CallbackReceipt)
            .where(CallbackReceipt.id == anchor["scan1_event_id"])
            .values(created_at=datetime.now(UTC) - timedelta(days=100))
        )
    assert await run_receipt_cleanup(flow.factory, 90, batch=1) == 5
    async with flow.factory() as session:
        assert list(await session.scalars(select(CallbackReceipt.id))) == [anchor["scan1_event_id"]]


@pytest.mark.parametrize("handed_off", [False, True])
async def test_task_close_releases_host_references_but_preserves_original_rack_responsibility(
    roller_lifecycle, monkeypatch, handed_off
):
    from app.businesses._components.bin_roller import retention

    flow = roller_lifecycle
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    face_id, cycle_id = await flow.ready_supply(count=1)
    if handed_off:
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FACE", face_id)
        await flow.finish_departure(cycle_id)
    feed = (await flow.all_rows("face_work"))[0]
    async with flow.factory() as session:
        all_ids = tuple(await session.scalars(select(ReliableMessage.id)))
        protected = await retention.protected_message_ids(session, candidate_ids=all_ids)
        assert (feed["feed_message_id"] in protected) is (not handed_off)
    async with flow.factory.begin() as session:
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "task-original"), reason="TASK_COMPLETED"
        )
    async with flow.factory() as session:
        assert feed["feed_message_id"] not in await retention.protected_message_ids(
            session, candidate_ids=all_ids
        )
    async with flow.factory.begin() as session:
        table = flow.roller.tables["face_work"]
        await session.execute(
            table.update()
            .where(table.c.id == face_id)
            .values(closed_at=datetime.now(UTC) - timedelta(days=100))
        )
        assert await flow.roller.cleanup_closed(
            session, cutoff=datetime.now(UTC) - timedelta(days=90)
        ) == int(handed_off)


async def test_finished_line_run_does_not_pin_the_last_point_forever(roller_current, monkeypatch):
    from app.businesses._components.bin_roller import retention
    from app.tasks.callback_tasks import run_receipt_cleanup

    flow = roller_current
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    flow.production.current_valid = False
    await flow.scan("SCAN1", "NOREAD", timestamp=10)
    passage = (await flow.current_rows("bin_passage"))[0]
    await flow.result(passage["scan1_command_code"], "SCAN1")
    await flow.advance_current("PASSAGE", passage["id"])
    assert (await flow.current_rows("workline_state"))[0]["line_run_id"] is None
    async with flow.factory.begin() as session:
        await session.execute(
            update(CallbackReceipt).values(created_at=datetime.now(UTC) - timedelta(days=100))
        )
    assert await run_receipt_cleanup(flow.factory, 90, batch=1) >= 1


async def test_superseded_orphan_has_no_permanent_current_runtime(roller_current):
    flow = roller_current
    await flow.scan("SCAN4", "A000000001-B", timestamp=10)
    old = (await flow.current_rows("bin_return"))[0]
    await flow.scan("SCAN3", "A000000001-B", timestamp=20)
    current = next(r for r in await flow.current_rows("bin_return") if r["id"] == old["id"])
    assert not current["is_current"] and current["close_reason"] == "SUPERSEDED"
    assert current["scan4_command_code"] is None


async def test_closed_member_and_face_stay_while_original_step_and_rack_need_them(
    roller_return, monkeypatch
):
    from app.businesses._components.bin_roller import retention

    flow = roller_return
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    await flow.bins(2)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
    await flow.complete_transport(face["current_transport_task_id"], count=1)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, face_id)
        closed = next(r for r in await flow.rows(session, "bin_return") if r["closed_at"])
        table = flow.roller.tables["bin_return"]
        await session.execute(
            table.update()
            .where(table.c.id == closed["id"])
            .values(closed_at=datetime.now(UTC) - timedelta(days=100))
        )
        candidates = (closed["scan3_event_id"], closed["scan4_event_id"])
        assert await retention.protected_event_ids(session, candidate_ids=candidates) == frozenset(
            candidates
        )
        assert (
            await flow.roller.cleanup_closed(session, cutoff=datetime.now(UTC) - timedelta(days=90))
            == 0
        )
    async with flow.factory.begin() as session:
        await flow.roller.repository(1).close(session, "face_work", face_id, "COMPLETED")
        table = flow.roller.tables["face_work"]
        await session.execute(
            table.update()
            .where(table.c.id == face_id)
            .values(closed_at=datetime.now(UTC) - timedelta(days=100))
        )
        # An active original rack still reads completed face checkpoints.
        assert (
            await flow.roller.cleanup_closed(session, cutoff=datetime.now(UTC) - timedelta(days=90))
            == 1
        )
        assert await flow.roller.original_record(session, "face_work", face_id)


async def test_wait_fact_protects_original_message_after_task_checkpoint_cleanup(
    roller_return, monkeypatch
):
    from app.businesses._components.bin_roller import retention
    from app.reliable.store import MessageStore

    flow = roller_return
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    await flow.bins(1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session)
        await flow.start(session, face_id)
        await flow.decide(session, face_id)
        await flow.consume_return(session, face_id)
        row = (await flow.rows(session, "bin_return"))[0]
        view = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=row["transport_task_id"],
            client_request_id=row["client_request_id"],
            caller=__import__(
                "app.integrations.wms_transport", fromlist=["TransportCaller"]
            ).TransportCaller(**row["caller"]),
        )
    store = MessageStore(flow.factory, lifecycles=flow.ecs.outbox.lifecycles)
    claims = await store.claim(100, flow.ecs.outbox.targets)
    claim = next(c for c in claims if c.message_id == view.submission_message_id)
    assert await store.before_send(claim, flow.ecs.outbox.targets[claim.message.target])
    async with flow.factory.begin() as session:
        await flow.roller.close_scope(
            session, ScopeRef("first", "TASK", "T1"), reason="TASK_COMPLETED"
        )
        original = await flow.roller.original_record(session, "bin_return", row["id"])
        assert original["return_state"] == "WAIT_FACT"
        assert view.submission_message_id in await retention.protected_message_ids(
            session, candidate_ids=(view.submission_message_id,)
        )


@pytest.mark.parametrize("answer", ["READY", "NO_BATCH"])
async def test_released_superseded_allocation_closes_resolved_round(roller_return, answer):
    flow = roller_return
    await flow.bins(2)
    old = (await flow.current_rows("bin_return"))[0]
    await flow.result(old["scan3_command_code"], "SCAN3")
    await flow.advance_current("RETURN", old["id"])
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind="SEGMENT")
        await flow.start(session, face_id)
    await flow.scan("SCAN3", old["bin_code"] + "-B", timestamp=100)
    async with flow.factory.begin() as session:
        await flow.decide(
            session, face_id, data={"result": "NO_BATCH"} if answer == "NO_BATCH" else None
        )
        await flow.consume_return(session, face_id)
        retired = await flow.roller.original_record(session, "bin_return", old["id"])
        assert retired["close_reason"] == "SUPERSEDED" and retired["selected_step_id"] is None
        cutoff = datetime.now(UTC) + timedelta(days=1)
        assert await flow.roller.cleanup_closed(session, cutoff=cutoff) >= 1


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
async def test_cleanup_and_new_passage_association_share_the_original_line_lock(
    roller_current, monkeypatch
):
    import asyncio

    from app.businesses._components.bin_roller import retention
    from app.tasks.callback_tasks import run_receipt_cleanup

    flow = roller_current
    monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    original = (await flow.current_rows("bin_passage"))[0]
    async with flow.factory.begin() as session:
        await session.execute(
            update(CallbackReceipt).values(created_at=datetime.now(UTC) - timedelta(days=100))
        )
    protected, release, entering = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = retention._lock_lines

    async def cleanup_lock(session, lines):
        await lock(session, lines)
        protected.set()
        await asyncio.wait_for(release.wait(), 5)

    monkeypatch.setattr(retention, "_lock_lines", cleanup_lock)
    cleanup = asyncio.create_task(run_receipt_cleanup(flow.factory, 90, batch=1))
    await asyncio.wait_for(protected.wait(), 5)
    scan_lock = flow.lock_scan_line

    async def receive_lock(session, line):
        entering.set()
        return await scan_lock(session, line)

    flow.lock_scan_line = receive_lock
    receiver = asyncio.create_task(flow.scan("SCAN2", "A000000001-C", timestamp=20))
    try:
        await asyncio.wait_for(entering.wait(), 5)
        assert not receiver.done()
    finally:
        release.set()
    removed, response = await asyncio.wait_for(asyncio.gather(cleanup, receiver), 10)
    assert removed == 0 and response.status_code == 200
    rows = await flow.current_rows("bin_passage")
    assert len(rows) == 1 and rows[0]["id"] == original["id"] and rows[0]["scan2_event_id"]
    async with flow.factory() as session:
        assert await session.get(CallbackReceipt, original["scan1_event_id"])
