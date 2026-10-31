from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.businesses._components.bin_roller.ports import RollerWorkRef, SourceFaceRef, SupplyChange
from app.businesses._components.bin_roller.service import BinRoller
from app.integrations.ecs_command.service import CommandOwner


@pytest.mark.asyncio
async def test_supply_change_keeps_original_task_and_member_revision(roller_store):
    store = roller_store
    roller = BinRoller(
        CommandOwner("first"), SimpleNamespace(), SimpleNamespace(), tables=store.tables
    )
    a = SourceFaceRef(1, "R1", "90")
    b = SourceFaceRef(2, "R1", "90")
    async with store.factory.begin() as session:
        await roller.apply_supply_change(session, SupplyChange(1, "T1", "M1", (a,), ()))
        await roller.apply_supply_change(session, SupplyChange(1, "T1", "M2", (a, b), ()))
        progress = await roller.get_supply_progress(session, work_line_id=1, task_id="T1")
        assert {p.source for p in progress} == {a, b}
        assert all(p.task_id == "T1" and p.pending and not p.feed_complete for p in progress)
        assert all(p.original_ids for p in progress)
        assert len((await session.execute(select(store.tables["supply_demand"]))).all()) == 2


@pytest.mark.asyncio
async def test_supply_progress_is_scoped_to_original_task(roller_store):
    store = roller_store
    roller = BinRoller(
        CommandOwner("first"), SimpleNamespace(), SimpleNamespace(), tables=store.tables
    )
    other = BinRoller(
        CommandOwner("second"), SimpleNamespace(), SimpleNamespace(), tables=store.tables
    )
    source = SourceFaceRef(1, "R1", "A")
    async with store.factory.begin() as session:
        for component, line, task in (
            (roller, 1, "T1"),
            (roller, 2, "T1"),
            (other, 1, "T1"),
            (roller, 1, "T2"),
        ):
            await component.apply_supply_change(
                session, SupplyChange(line, task, "M1", (source,), ())
            )
        progress = await roller.get_supply_progress(session, work_line_id=1, task_id="T1")
        assert len(progress) == 1
        assert progress[0].task_id == "T1"
        assert await roller.get_supply_progress(session, work_line_id=1, task_id="missing") == ()


@pytest.mark.asyncio
async def test_supply_replay_keeps_original_cycle_reference(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        first = await env.demand(session)
        first_service, cycle_id = await env.ready(session, first)
        second = await env.demand(session, revision=2, change="M2")
        second_service, same_cycle = await env.ready(session, second, cycle_id=cycle_id)
        assert same_cycle == cycle_id
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "M1", (SourceFaceRef(1, "R1", "A"),), ())
        )
        await env.roller.apply_supply_change(
            session, SupplyChange(1, "T1", "C1", (), (SourceFaceRef(1, "R1", "A"),))
        )
        rows = sorted(await env.rows(session, "supply_demand"), key=lambda r: r["plan_revision"])
        assert [(r["cycle_id"], r["service_id"], r["withdrawn"]) for r in rows] == [
            (cycle_id, first_service, True),
            (cycle_id, second_service, False),
        ]
        assert len(await env.rows(session, "rack_action")) == 1
        assert len(await env.rows(session, "rack_cycle")) == 1
        other = await env.demand(session, task="T2")
        with pytest.raises(ValueError, match="association"):
            await env.roller.repository(1).bind_supply(
                session, demand_id=other["id"], cycle_id=cycle_id
            )


@pytest.mark.asyncio
async def test_binding_replay_does_not_remove_original_face(roller_feed):
    env = roller_feed
    async with env.store.factory.begin() as session:
        original = await env.demand(session)
        service_id, cycle_id = await env.ready(session, original)
        await env.roller.repository(1).bind_supply(
            session, demand_id=original["id"], cycle_id=cycle_id
        )
        assert (await env.rows(session, "supply_demand"))[0]["service_id"] == service_id


@pytest.mark.asyncio
async def test_pending_feed_discovers_original_demand_before_cycle_and_retains_in_progress(
    roller_feed,
):
    env = roller_feed
    async with env.store.factory.begin() as session:
        first = await env.demand(session)
        stopped = await env.demand(session, revision=2, change="M2")
        started = await env.demand(session, revision=3, change="M3")
        await env.ready(session, started)
        await env.roller.apply_supply_change(
            session,
            SupplyChange(
                1, "T1", "C1", (), (SourceFaceRef(2, "R1", "A"), SourceFaceRef(3, "R1", "A"))
            ),
        )
        upper = await env.roller.get_pending_upper_id(session, kind="FEED")
        later = await env.demand(session, revision=4, change="M4")
        page = await env.roller.list_pending(
            session, kind="FEED", after_id=None, upper_id=upper, limit=1
        )
        assert page == (RollerWorkRef("FEED", first["id"]),)
        page = await env.roller.list_pending(
            session, kind="FEED", after_id=first["id"], upper_id=upper, limit=10
        )
        assert page == (RollerWorkRef("FEED", started["id"]),)
        assert stopped["id"] not in {r.record_id for r in page}
        assert later["id"] > upper
        assert await env.consume_feed(session, first["id"]) == "WAITING_FOR_SELECTED_FACE"
