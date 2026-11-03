import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import update

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
from app.models import WorkLine


async def set_limit(flow, limit):
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == 1).values(five_rack_request_limit=limit)
        )


async def entries(flow):
    return [a for a in await flow.all_rows("rack_action") if a["kind"] == "CTU01"]


async def ordered_waiting_faces(flow):
    from app.core.identifiers import uuid7

    # FEED B must select A first in these scenarios, even when both IDs share a millisecond.
    with patch(
        "app.businesses._components.bin_roller.repository.uuid7",
        side_effect=sorted((uuid7(), uuid7())),
    ):
        first = await flow.demand(rack="WAITING", face="A")
        second = await flow.demand(rack="WAITING", face="B")
    return first, second


@pytest.mark.parametrize("limit,expected", [(None, 5), (3, 3)])
async def test_original_requests_share_current_line_budget_and_keep_identity(
    roller_lifecycle, limit, expected
):
    flow = roller_lifecycle
    await set_limit(flow, limit)
    demands = [await flow.demand(rack=f"R{i}") for i in range(5)]
    for demand in demands:
        await flow.advance("FEED", demand)
    original = await entries(flow)
    assert len(original) == expected
    await asyncio.gather(*(flow.advance("FEED", demand) for demand in demands))
    assert [a["id"] for a in await entries(flow)] == [a["id"] for a in original]
    assert len({a["transport_task_id"] for a in await entries(flow)}) == expected


@pytest.mark.parametrize("status", [None, "FAILED"])
async def test_unknown_or_failed_entry_keeps_budget_after_limit_is_lowered(
    roller_lifecycle, status
):
    from app.core.identifiers import uuid7

    flow = roller_lifecycle
    await set_limit(flow, 2)
    # The final inserted demand must remain the quota waiter when IDs share a millisecond.
    with patch(
        "app.businesses._components.bin_roller.repository.uuid7",
        side_effect=sorted(uuid7() for _ in range(3)),
    ):
        demands = [await flow.demand(rack=f"R{i}") for i in range(3)]
    for demand in demands:
        await flow.advance("FEED", demand)
    original = await entries(flow)
    assert len(original) == 2
    if status:
        await flow.rack_result(original[0], status=status)
        await flow.advance("RACK_CYCLE", original[0]["cycle_id"])
    await set_limit(flow, 1)
    for demand in demands:
        await flow.advance("FEED", demand)
    assert [a["id"] for a in await entries(flow)] == [a["id"] for a in original]
    await set_limit(flow, 3)
    await flow.advance("FEED", demands[-1])
    assert len(await entries(flow)) == 3


async def test_selected_quota_waiter_can_be_withdrawn_without_starting_or_stalling_sibling(
    roller_lifecycle,
):
    flow = roller_lifecycle
    await set_limit(flow, 1)
    first = await flow.demand(rack="OCCUPIED")
    await flow.advance("FEED", first)
    withdrawn = await flow.demand(rack="WAITING", face="A")
    await flow.advance("FEED", withdrawn)
    demand = next(d for d in await flow.all_rows("supply_demand") if d["id"] == withdrawn)
    assert demand["cycle_id"] is not None
    assert len(await entries(flow)) == 1
    sibling = await flow.demand(rack="WAITING", face="B")
    other = await flow.demand(rack="OTHER")
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(1, flow.current_task, "withdraw", (), (SourceFaceRef(1, "WAITING", "A"),)),
        )
    await set_limit(flow, 3)
    for kind, record in (
        ("RACK_CYCLE", demand["cycle_id"]),
        ("FEED", withdrawn),
        ("FEED", sibling),
        ("FEED", other),
    ):
        await flow.advance(kind, record)
    actions = await entries(flow)
    assert len(actions) == 3
    cycles = await flow.all_rows("rack_cycle")
    original_cycle = next(c for c in cycles if c["id"] == demand["cycle_id"])
    assert original_cycle["status"] == "ACTIVE" and original_cycle["release_basis"] is None
    cancelled = next(d for d in await flow.all_rows("face_work") if d["id"] == withdrawn)
    assert cancelled["service_state"] == "WITHDRAWN"
    assert cancelled["cycle_id"] == demand["cycle_id"]
    assert cancelled["service_id"] == demand["service_id"]
    waiting_cycle = next(c for c in cycles if c["rack_id"] == "WAITING" and c["status"] == "ACTIVE")
    waiting = next(a for a in actions if a["cycle_id"] == waiting_cycle["id"])
    assert waiting["target_face"] == "B"
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=1, task_id=flow.current_task
        )
        old_source = next(
            p for p in progress if p.source.rack_id == "WAITING" and p.source.rack_face == "A"
        )
        assert not old_source.feed_complete and not old_source.pending


@pytest.mark.parametrize(
    "withdrawn,persisted",
    [
        ("A", False),
        ("B", False),
        ("A", True),
        ("B", True),
    ],
)
async def test_prebound_sibling_cancellation_preserves_unstarted_identities_and_valid_face(
    roller_lifecycle,
    withdrawn,
    persisted,
):
    from app.core.identifiers import uuid7

    flow = roller_lifecycle
    await set_limit(flow, 1)
    occupied = await flow.demand(rack="OCCUPIED")
    await flow.advance("FEED", occupied)
    first, second = await ordered_waiting_faces(flow)
    if persisted:
        # Previously saved unstarted candidate: FEED B selected the older service A.
        async with flow.factory.begin() as session:
            repo = flow.roller.repository(1)
            from app.businesses._components.bin_roller.rack_flow import register_job

            cycle_id = await register_job(
                flow.roller,
                session,
                repo,
                "ROLLER_ORDINARY",
                second,
                "WAITING",
                "B",
                flow.current_task,
                await flow.roller.read_resources(session, 1),
            )
            await repo.bind_supply(session, demand_id=second, cycle_id=cycle_id)
            await repo.bind_supply(
                session,
                demand_id=first,
                cycle_id=cycle_id,
                service_id=str(uuid7()),
            )
    else:
        await flow.advance("FEED", second)
    old = next(d for d in await flow.all_rows("supply_demand") if d["id"] == first)
    old_cycle = next(c for c in await flow.all_rows("rack_cycle") if c["id"] == old["cycle_id"])
    assert old["service_id"] and len(await entries(flow)) == 1
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1, flow.current_task, "withdraw", (), (SourceFaceRef(1, "WAITING", withdrawn),)
            ),
        )
    valid = second if withdrawn == "A" else first
    cancelled = first if withdrawn == "A" else second
    await set_limit(flow, 2)
    for kind, identity in (
        ("RACK_CYCLE", old_cycle["id"]),
        ("FEED", cancelled),
        ("FEED", valid),
        ("FEED", valid),
    ):
        await flow.advance(kind, identity)
    cycles = await flow.all_rows("rack_cycle")
    actions = await entries(flow)
    faces = [
        a["target_face"]
        for a in actions
        if next(c for c in cycles if c["id"] == a["cycle_id"])["rack_id"] == "WAITING"
    ]
    assert faces == (["B"] if withdrawn == "A" else ["A"])
    saved = next(d for d in await flow.all_rows("face_work") if d["id"] == first)
    assert saved["cycle_id"] == old["cycle_id"] and saved["service_id"] == old["service_id"]
    saved_cycle = next(c for c in cycles if c["id"] == old_cycle["id"])
    assert saved_cycle["source_ref"] == old_cycle["source_ref"]
    assert saved_cycle["status"] == "ACTIVE" and saved_cycle["release_basis"] is None
    if withdrawn == "A":
        assert saved["service_state"] == "WITHDRAWN"
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session,
            work_line_id=1,
            task_id=flow.current_task,
        )
        cancelled_progress = next(
            p for p in progress if p.source.rack_id == "WAITING" and p.source.rack_face == withdrawn
        )
        assert not cancelled_progress.feed_complete and not cancelled_progress.pending
    other = await flow.demand(rack="OTHER")
    await set_limit(flow, 3)
    await flow.advance("FEED", other)
    assert len(await entries(flow)) == 3
    if not persisted:
        assert old_cycle["source_ref"] == first


async def test_withdrawal_after_original_entry_keeps_started_obligation(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await entries(flow))[0]
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(1, flow.current_task, "withdraw", (), (SourceFaceRef(1, "R1", "A"),)),
        )
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    current = next(d for d in await flow.all_rows("face_work") if d["id"] == demand)
    assert current["service_state"] == "PENDING"
    await flow.rack_result(entry)
    await flow.advance("FEED", demand)
    current = next(d for d in await flow.all_rows("face_work") if d["id"] == demand)
    assert current["feed_id"] is not None and current["service_state"] == "READY"
    async with flow.factory() as session:
        progress = (
            await flow.roller.get_supply_progress(
                session,
                work_line_id=1,
                task_id=flow.current_task,
            )
        )[0]
        assert not progress.feed_complete and progress.pending


async def test_unstarted_withdrawn_service_remains_terminal_after_sibling_cycle_releases(
    roller_lifecycle,
):
    flow = roller_lifecycle
    await set_limit(flow, 1)
    occupied = await flow.demand(rack="OCCUPIED")
    await flow.advance("FEED", occupied)
    first, second = await ordered_waiting_faces(flow)
    await flow.advance("FEED", second)
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(1, flow.current_task, "withdraw", (), (SourceFaceRef(1, "WAITING", "A"),)),
        )
    await set_limit(flow, 2)
    await flow.advance("FEED", first)
    entry = (await entries(flow))[-1]
    assert entry["target_face"] == "B"
    await flow.rack_result(entry)
    await flow.advance("FEED", second)
    feed = (await flow.all_rows("feed"))[-1]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", second)
    await flow.finish_departure(entry["cycle_id"])
    cancelled = next(d for d in await flow.all_rows("face_work") if d["id"] == first)
    assert cancelled["service_state"] == "WITHDRAWN"
    async with flow.factory() as session:
        upper = await flow.roller.get_pending_upper_id(session, kind="FEED")
        pending = await flow.roller.list_pending(
            session,
            kind="FEED",
            after_id=None,
            upper_id=upper,
            limit=100,
        )
        assert first not in [p.record_id for p in pending]
        progress = await flow.roller.get_supply_progress(
            session,
            work_line_id=1,
            task_id=flow.current_task,
        )
        original = next(
            p for p in progress if p.source.rack_id == "WAITING" and p.source.rack_face == "A"
        )
        assert not original.feed_complete and not original.pending


@pytest.mark.parametrize(
    "buffer,first_work",
    [("queued", "RACK_CYCLE"), ("empty", "RACK_CYCLE"), ("empty", "DRAIN"), ("unknown", "DRAIN")],
)
async def test_ordinary_and_drain_original_requests_use_one_budget(
    roller_lifecycle, buffer, first_work
):
    flow = roller_lifecycle
    await set_limit(flow, 1)
    demand = await flow.demand(rack="ORDINARY")
    await flow.advance("FEED", demand)
    entry = (await entries(flow))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    drain = (await flow.all_rows("drain"))[0]
    await flow.decision(
        "drain",
        drain["id"],
        dict(
            result="READY",
            racks=[dict(rack_id="DRAIN", rack_face=["A"])],
        ),
    )
    await flow.advance("DRAIN", drain["id"])
    assert len(await entries(flow)) == 1
    cycle = next(c for c in await flow.all_rows("rack_cycle") if c["purpose"] == "DRAIN")
    service = next(s for s in await flow.all_rows("face_work") if s["cycle_id"] == cycle["id"])
    original = (await flow.all_rows("drain"))[0]
    if buffer == "queued":
        await flow.finish_departure(entry["cycle_id"])
        for _ in range(2):
            await flow.advance("RACK_CYCLE", cycle["id"])
        assert [a["source_ref"] for a in await entries(flow) if a["cycle_id"] == cycle["id"]] == [
            service["service_id"]
        ]
        return

    # Another ready face takes the last box. Its original Transport decides when it leaves.
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session))
        await flow.decide(session, batch_id)
        assert await flow.consume_return(session, batch_id) == "WAITING_FOR_RETURN_RESULT"
    await flow.advance("DRAIN", drain["id"])
    assert (await flow.all_rows("drain"))[0]["unused_racks"] == []
    await flow.success(
        batch_id, placed=buffer == "empty", status="SUCCEEDED" if buffer == "empty" else "UNKNOWN"
    )
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == (
            "SUCCEEDED" if buffer == "empty" else "UNKNOWN"
        )
        assert await flow.roller.return_channel_empty(session, work_line_id=1) == (
            buffer == "empty"
        )
    if buffer == "unknown":
        # A new visit retires the FIFO passage while its original movement remains unresolved.
        await flow.scan("SCAN3", "A000000002-B", timestamp=50)
        await flow.scan("SCAN3", "A000000001-B", timestamp=51)
        async with flow.factory() as session:
            assert not await flow.roller.get_return_candidates(session, work_line_id=1)
            assert not await flow.roller.return_channel_empty(session, work_line_id=1)
        await flow.finish_departure(entry["cycle_id"])
        await flow.advance("RACK_CYCLE", cycle["id"])
        assert (await flow.all_rows("drain"))[0]["unused_racks"] == []
        assert not [a for a in await flow.all_rows("rack_action") if a["cycle_id"] == cycle["id"]]
        assert next(c for c in await flow.all_rows("rack_cycle") if c["id"] == cycle["id"])[
            "pending"
        ]
        return

    await set_limit(flow, 2)
    await flow.advance(first_work, cycle["id"] if first_work == "RACK_CYCLE" else drain["id"])
    await flow.finish_departure(entry["cycle_id"])
    for _ in range(2):
        await flow.advance("RACK_CYCLE", cycle["id"])
        await flow.advance("DRAIN", drain["id"])
    current = (await flow.all_rows("drain"))[0]
    assert (current["status"], current["pending"], current["unused_racks"]) == (
        "COMPLETED",
        False,
        ["DRAIN"],
    )
    assert all(current[k] == original[k] for k in ("id", "operation_id", "message_id", "decision"))
    assert not [a for a in await flow.all_rows("rack_action") if a["cycle_id"] == cycle["id"]]
    saved = next(c for c in await flow.all_rows("rack_cycle") if c["id"] == cycle["id"])
    assert (saved["status"], saved["pending"], saved["release_basis"]) == ("WITHDRAWN", False, None)
    face = next(
        s for s in await flow.all_rows("face_work") if s["service_id"] == service["service_id"]
    )
    assert face["service_state"] == "WITHDRAWN" and face["cycle_id"] == cycle["id"]


@pytest.mark.parametrize("response", [None, (503, "UNAVAILABLE"), (200, "RECEIVED")])
async def test_departure_without_valid_acceptance_does_not_admit_waiter(roller_lifecycle, response):
    flow = roller_lifecycle
    await set_limit(flow, 1)
    demand, cycle_id = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(
            result="READY",
            rack_destination=dict(type="ZONE", location_code="STORAGE"),
        ),
    )
    await flow.advance("RACK_CYCLE", cycle_id)
    action = (await flow.all_rows("rack_action"))[-1]
    if response:
        http, code = response
        await flow.accepted(action, http=http, code=code)
    waiter = await flow.demand(rack="WAITING")
    await flow.advance("RACK_CYCLE", cycle_id)
    await flow.advance("FEED", waiter)
    assert len(await entries(flow)) == 1
    cycle = next(c for c in await flow.all_rows("rack_cycle") if c["id"] == cycle_id)
    assert cycle["status"] == "ACTIVE" and cycle["release_basis"] is None


async def test_other_owner_original_entry_does_not_use_this_owners_budget(roller_lifecycle):
    from dataclasses import replace

    from app.businesses._components.rack_dispatch.contracts import JobOrigin
    from app.businesses._components.rack_dispatch.service import RackDispatch
    from app.core.identifiers import uuid7
    from app.integrations.wms_transport import TransportCaller, TransportService

    flow = roller_lifecycle
    await set_limit(flow, 1)
    caps = flow.roller.capabilities
    other = RackDispatch(
        "other-owner",
        replace(
            caps,
            transport=TransportService(
                caps.transport.outbox, target=caps.transport.target, owner_domain="other-owner"
            ),
        ),
    )
    async with flow.factory.begin() as session:
        cycle = await other.register(
            session,
            JobOrigin(
                "ROLLER_ORDINARY",
                "OLD",
                1,
                "OTHER",
                "T",
                "LINE-1",
                "FIVE",
                "A",
                TransportCaller("1"),
                str(uuid7()),
                {},
            ),
        )
        assert await other.prepare(session, job_id=cycle, kind="ENTER", source_ref="OLD", face="A")
    own = await flow.demand(rack="OWN")
    await flow.advance("FEED", own)
    assert len([a for a in await entries(flow) if a["owner_domain"] == "first"]) == 1


async def test_new_face_does_not_reactivate_withdrawn_original(roller_lifecycle):
    flow = roller_lifecycle
    await set_limit(flow, 1)
    occupied = await flow.demand(rack="OCCUPIED")
    await flow.advance("FEED", occupied)
    old = await flow.demand()
    await flow.advance("FEED", old)
    original = next(c["id"] for c in await flow.all_rows("rack_cycle") if c["rack_id"] == "R1")
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(1, flow.current_task, "withdraw", (), (SourceFaceRef(1, "R1", "A"),)),
        )
    await flow.advance("RACK_CYCLE", original)
    new = await flow.demand(revision=2)
    await set_limit(flow, 2)
    await flow.advance("FEED", new)
    async with flow.factory() as session:
        historical = await flow.roller.rack_dispatch.progress(session, job_id=original)
        assert historical.state == "WITHDRAWN" and not historical.actions
    assert original not in [a["cycle_id"] for a in await entries(flow)]
    assert len(await entries(flow)) == 2


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
async def test_two_first_entries_share_last_line_slot(roller_lifecycle):
    from sqlalchemy import select

    flow = roller_lifecycle
    await set_limit(flow, 1)
    demands = [await flow.demand(rack=name) for name in ("R1", "R2")]
    initial = asyncio.Barrier(2)
    attempts = []

    async def overlap(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.scalar(select(WorkLine.id))
            await asyncio.wait_for(initial.wait(), 5)

    await asyncio.wait_for(
        asyncio.gather(*(flow.advance("FEED", d, before=overlap) for d in demands)), 10
    )
    assert 1 in attempts
    assert len(await entries(flow)) == 1
    assert len(await flow.all_rows("rack_cycle")) == 2
