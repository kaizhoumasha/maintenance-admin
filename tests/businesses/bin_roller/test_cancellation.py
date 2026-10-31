import pytest

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange


@pytest.mark.asyncio
async def test_cancel_unstarted_demand_stops_new_allocation(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
        assert await env.consume_feed(session, original["id"]) == "WITHDRAWN"
        assert await env.rows(session, "feed") == []
        progress = (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[0]
        assert not progress.feed_complete and not progress.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("before_request", [True, False])
async def test_cancelled_in_progress_rack_requests_wms_on_arrival(roller_feed, before_request):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        service_id, _ = await env.ready(session, original)
        services = env.store.tables["face_service"]
        if before_request:
            await session.execute(
                services.update().where(services.c.id == service_id).values(status="PENDING")
            )
            assert await env.start(session, original) is None
        if not before_request:
            feed_id = await env.start(session, original)
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
        if before_request:
            await session.execute(
                services.update().where(services.c.id == service_id).values(status="READY")
            )
        recovered = await env.start(session, original)
        if not before_request:
            assert recovered == feed_id
        await env.decide(session, recovered)
        await env.consume_feed(session, original["id"])
        assert len(await env.rows(session, "feed_segment")) == 1
        progress = (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1"))[0]
        assert progress.pending and not progress.feed_complete


@pytest.mark.asyncio
async def test_cancel_after_ready_keeps_remaining_segments(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original)
        feed_id = await env.start(session, original)
        await env.decide(session, feed_id)
        await env.consume_feed(session, original["id"])
        first = (await env.rows(session, "feed_segment"))[0]
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
    await env.success(first)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        opportunity = (await env.rows(session, "return_opportunity"))[0]
        table = env.store.tables["return_opportunity"]
        await session.execute(
            table.update()
            .where(table.c.id == opportunity["id"])
            .values(ended=True, end_reason="NO_CANDIDATES", end_evidence={})
        )
        await env.consume_feed(session, original["id"])
        segments = sorted(await env.rows(session, "feed_segment"), key=lambda r: r["segment_index"])
        assert [m["bin_code"] for m in segments[1]["members"]] == ["A000000005"]
        assert len(await env.rows(session, "feed")) == 1


@pytest.mark.asyncio
async def test_late_ready_belongs_to_original_segment(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        service_id, cycle_id = await env.ready(session, original)
        feed_id = await env.start(session, original)
        old_feed = dict((await env.rows(session, "feed"))[0])
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
        newer = await env.demand(session, revision=2, change="M2")
        await env.ready(session, newer, cycle_id=cycle_id)
        await env.demand(session, task="T2", change="M3")
    async with env.store.factory.begin() as session:
        await env.decide(session, feed_id, count=1)
        await env.consume_feed(session, original["id"])
        await env.consume_feed(session, newer["id"])
        feeds = await env.rows(session, "feed")
        assert len(feeds) == 1
        assert feeds[0]["service_id"] == service_id
        assert feeds[0]["operation_id"] == old_feed["operation_id"]
        assert feeds[0]["message_id"] == old_feed["message_id"]
        segment = (await env.rows(session, "feed_segment"))[0]
        assert segment["feed_id"] == feed_id
    await env.success(segment)
    async with env.store.factory.begin() as session:
        await env.consume_feed(session, original["id"])
        progress = await env.roller.get_supply_progress(session, work_line_id=1, task_id="T1")
        by_revision = {p.source.plan_revision: p for p in progress}
        assert by_revision[1].feed_complete and not by_revision[1].pending
        assert not by_revision[2].feed_complete and by_revision[2].pending
        assert not (await env.roller.get_supply_progress(session, work_line_id=1, task_id="T2"))[
            0
        ].feed_complete


@pytest.mark.asyncio
async def test_cancelled_ready_face_without_original_entry_never_starts(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        await env.ready(session, original, in_progress=False)
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
        assert await env.start(session, original) is None
        assert await env.rows(session, "feed") == []
