"""BAD_CASES SCAN3 facts adapted to current ports; times/outcomes are synthetic stimuli."""

import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.businesses._components.bin_roller.service import BinRoller
from app.models import CallbackReceipt, ReliableMessage

BAD = json.loads((Path(__file__).parent / "fixtures/wes_recorded_cases.json").read_text())["bad"]
OTHER = "A000002701-B"  # GOOD's recorded next bin; combined scenario, not a raw event trace.


async def rows(flow, key):
    async with flow.factory() as session:
        return await flow.rows(session, key)


async def arrival(flow, point, raw):
    return next(
        a
        for a in reversed(await rows(flow, "scan_arrival"))
        if a["point"] == point and a["raw_bin_code"] == raw
    )


async def candidates(flow):
    async with flow.factory() as session:
        return await flow.roller.get_return_candidates(session, work_line_id=1)


async def rebuild(flow):
    old = flow.roller
    flow.roller = BinRoller(old.context, old.capabilities, old.production, tables=flow.store.tables)
    flow.runtime.services[old.context.domain].components["bin_roller"] = flow.roller
    async with flow.factory.begin() as session:
        await session.execute(
            delete(CallbackReceipt).where(CallbackReceipt.device_code.in_(flow.devices.values()))
        )


async def visit(flow, raw, timestamp):
    await flow.scan("SCAN3", raw, timestamp=timestamp)
    await flow.scan("SCAN4", raw, timestamp=timestamp + 1)
    return await arrival(flow, "SCAN4", raw)


async def test_recorded_new_scan3_supersedes_old_unassociated_scan4_queue_shadow(roller_scans):
    flow = roller_scans
    raw = BAD["scan3_reentry"]["raw"]
    await flow.scan("SCAN4", raw, timestamp=1)
    orphan = dict(await arrival(flow, "SCAN4", raw))
    assert orphan["return_round_id"] is None and orphan["command_code"] is None
    await flow.scan("SCAN3", raw, timestamp=10)
    other4 = await visit(flow, OTHER, 20)
    await flow.scan("SCAN4", raw, timestamp=30)
    own4 = await arrival(flow, "SCAN4", raw)
    await flow.result(own4["command_code"], "SCAN4")
    assert await candidates(flow) == ()
    await flow.result(other4["command_code"], "SCAN4")
    await rebuild(flow)
    await flow.scan("SCAN4", raw, timestamp=1)
    assert [r["id"] for r in await candidates(flow)] == [
        other4["return_round_id"],
        own4["return_round_id"],
    ]
    async with flow.factory() as session:
        assert dict(await flow.roller.original_arrival(session, orphan["id"])) == orphan


async def return_opportunity(flow, kind="FACE_END"):
    if kind == "PURE_RETURN":
        flow.current_task, flow.successor = None, "NONE"
        scan4 = next(a for a in await rows(flow, "scan_arrival") if a["point"] == "SCAN4")
        await flow.advance("SCAN", scan4["id"])
        drain = (await rows(flow, "drain"))[0]
        await flow.decision(
            "drain", drain["id"], dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A"])])
        )
        await flow.advance("DRAIN", drain["id"])
        entry = (await rows(flow, "rack_action"))[0]
        await flow.rack_result(entry)
        await flow.advance("RACK_CYCLE", entry["cycle_id"])
        return entry
    if kind == "SEGMENT":
        demand, _ = await flow.ready_supply(count=5)
        await flow.feed_success((await rows(flow, "feed_segment"))[0])
        await flow.advance("FEED", demand)
        return (await rows(flow, "rack_action"))[0]
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await rows(flow, "rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await rows(flow, "feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    return entry


async def test_recorded_scan3_reentry_rolls_back_and_does_not_separate_scan4(roller_scans):
    flow = roller_scans
    raw = BAD["scan3_reentry"]["raw"]
    old4 = await visit(flow, raw, 10)
    old3 = await arrival(flow, "SCAN3", raw)
    await flow.result(old3["command_code"], "SCAN3")
    async with flow.factory() as session:
        transaction = await session.begin()
        await flow.scan("SCAN3", raw, timestamp=30, session=session)
        assert len(await flow.rows(session, "return_round")) == 2
        await transaction.rollback()
    assert len(await rows(flow, "return_round")) == 1
    assert len(await rows(flow, "scan_fact")) == 2
    async with flow.factory() as session:
        assert len(await flow.messages(session)) == 2
    await rebuild(flow)
    await flow.scan("SCAN3", raw, timestamp=30)
    new3 = await arrival(flow, "SCAN3", raw)
    assert new3["command_code"] not in {None, old3["command_code"]}
    await flow.scan("SCAN3", raw, timestamp=31)
    assert (await arrival(flow, "SCAN3", raw))["id"] == new3["id"]
    await flow.scan("SCAN4", raw, timestamp=32)
    assert (await arrival(flow, "SCAN4", raw))["id"] == old4["id"]
    await flow.result(old4["command_code"], "SCAN4")
    assert await candidates(flow) == ()
    await flow.scan("SCAN4", raw, timestamp=33)
    own4 = await arrival(flow, "SCAN4", raw)
    assert own4["return_round_id"] == new3["return_round_id"]
    assert own4["command_code"] not in {None, old4["command_code"]}
    await flow.result(own4["command_code"], "SCAN4")
    assert [r["id"] for r in await candidates(flow)] == [new3["return_round_id"]]


@pytest.mark.parametrize("foreign_history", [False, True])
async def test_recorded_independent_scan3_requires_only_own_scan4_success(
    roller_scans, foreign_history
):
    flow = roller_scans
    raw = BAD["scan3_cancelled"]["raw"]
    flow.production.current_valid = False
    if foreign_history:
        flow.work_line_id = 2
        foreign = await visit(flow, raw, 1)
        flow.work_line_id = 1
    own = await visit(flow, raw, 10)
    assert own["command_code"] is not None
    assert len(await rows(flow, "scan_fact")) == (4 if foreign_history else 2)
    assert await candidates(flow) == ()
    if foreign_history:
        await flow.result(foreign["command_code"], "SCAN4")
        assert await candidates(flow) == ()
    scan3 = await arrival(flow, "SCAN3", raw)
    await flow.result(scan3["command_code"], "SCAN3")
    assert await candidates(flow) == ()
    await flow.result(own["command_code"], "SCAN4")
    assert [r["bin_code"] for r in await candidates(flow)] == [raw[:-2]]


@pytest.mark.parametrize("old_status", ["SUCCESS", "FAILED"])
@pytest.mark.parametrize("old_result_before", [False, True])
async def test_recorded_reentry_preserves_old_facts_and_requires_current_fifo_success(
    roller_scans, old_status, old_result_before
):
    flow = roller_scans
    raw = BAD["scan3_reentry"]["raw"]
    old4 = await visit(flow, raw, 10)
    old3 = await arrival(flow, "SCAN3", raw)
    old_round = dict((await rows(flow, "return_round"))[0])
    async with flow.factory() as session:
        frozen = {}
        for old in (old3, old4):
            command = await flow.command(session, old["command_code"])
            frozen[command.command_code] = (command.expires_at, command.request_raw)
    if old_result_before:
        for old in (old3, old4):
            await flow.result(old["command_code"], old["point"], status=old_status)
    # Each point gets a real intervening bin: SCAN3 is not a SCAN4 visit separator.
    other4 = await visit(flow, OTHER, 20)
    new4 = await visit(flow, raw, 30)
    new3 = await arrival(flow, "SCAN3", raw)
    assert new3["command_code"] is not None
    assert new3["return_round_id"] != old_round["id"]
    assert new4["return_round_id"] == new3["return_round_id"]
    assert new4["command_code"] not in {None, old4["command_code"]}
    assert await candidates(flow) == ()  # Unready B remains the physical head.
    await rebuild(flow)
    for _ in range(2):
        for old in (old3, old4):
            await flow.result(old["command_code"], old["point"], status=old_status)
            await flow.scan(old["point"], raw, timestamp=old["scanned_at"])
            async with flow.factory.begin() as session:
                await flow.roller.advance(session, RollerWorkRef("SCAN", old["id"]))
        assert await candidates(flow) == ()
    assert dict((await rows(flow, "return_round"))[0]) == old_round
    await flow.result(new3["command_code"], "SCAN3")
    assert await candidates(flow) == ()
    await flow.result(other4["command_code"], "SCAN4")
    assert [r["bin_code"] for r in await candidates(flow)] == [OTHER[:-2]]
    await flow.result(new4["command_code"], "SCAN4")
    assert [r["bin_code"] for r in await candidates(flow)] == [OTHER[:-2], raw[:-2]]
    assert len(await rows(flow, "return_round")) == 3
    assert len(await rows(flow, "scan_arrival")) == 6
    async with flow.factory() as session:
        for code, original in frozen.items():
            command = await flow.command(session, code)
            assert (command.expires_at, command.request_raw) == original


@pytest.mark.parametrize("old_transport", ["WAITING_WMS", "INFLIGHT", "CANCELLED", "LATE_FAILED"])
async def test_recorded_reentry_retains_native_obligation_without_moving_new_round(
    roller_lifecycle, old_transport
):
    flow = roller_lifecycle
    case = BAD["scan3_cancelled"]
    raw = case["raw"]
    old4 = await visit(flow, raw, 10)
    await flow.result(old4["command_code"], "SCAN4")
    entry = await return_opportunity(flow)
    batch_id = (await rows(flow, "return_batch"))[0]["id"]
    async with flow.factory.begin() as session:
        if old_transport != "WAITING_WMS":
            await flow.decide(session, batch_id)
    if old_transport != "WAITING_WMS":
        await flow.advance("RETURN_BATCH", batch_id)
    async with flow.factory.begin() as session:
        batch = dict(await flow.roller.original_record(session, "return_batch", batch_id))
        old_round = dict((await flow.rows(session, "return_round"))[0])
        frozen_request = await session.scalar(
            select(ReliableMessage.request_raw).where(ReliableMessage.id == batch["message_id"])
        )
        frozen_transport = await session.scalar(
            select(ReliableMessage.request_raw).where(
                ReliableMessage.operation_id == batch["transport_operation_id"]
            )
        )
    # Synthetic current callback: archived OPERATOR_CONFIRMED_CANCELLED maps to MANUAL_ABORTED.
    cancelled = dict(
        transport_task_id=batch["transport_task_id"],
        kind="BIN_MOVE",
        outcome_revision=1,
        results=[
            dict(
                container_id=case["bin_code"],
                status="FAILED",
                failure_code="MANUAL_ABORTED",
                final_position=dict(
                    kind="HANDOFF_POSITION", location_code=batch["source"]["location_code"]
                ),
            )
        ],
    )
    if old_transport == "CANCELLED":
        await flow.rack_callback(cancelled)
        await flow.advance("RETURN_BATCH", batch_id)
        assert (await rows(flow, "return_batch"))[0]["status"] == "FAILED"
    other4 = await visit(flow, OTHER, 20)
    new4 = await visit(flow, raw, 30)
    assert new4["return_round_id"] != old_round["id"]
    assert new4["command_code"] is not None
    await rebuild(flow)
    if old_transport == "WAITING_WMS":
        async with flow.factory.begin() as session:
            await flow.decide(session, batch_id)
        for _ in range(2):
            await flow.advance("RETURN_BATCH", batch_id)
        current = (await rows(flow, "return_batch"))[0]
        assert current["transport_task_id"] is None
    elif old_transport == "INFLIGHT":
        for _ in range(2):
            await flow.success(batch_id)
            await flow.advance("RETURN_BATCH", batch_id)
        assert (await rows(flow, "return_round"))[0]["status"] == "COMPLETED"
    else:
        for _ in range(2):
            await flow.rack_callback(cancelled)
            await flow.advance("RETURN_BATCH", batch_id)
        assert dict((await rows(flow, "return_round"))[0]) == old_round
        assert (await rows(flow, "return_batch"))[0]["status"] == "FAILED"
    assert await candidates(flow) == ()
    await flow.result(other4["command_code"], "SCAN4")
    assert [r["bin_code"] for r in await candidates(flow)] == [OTHER[:-2]]
    await flow.result(new4["command_code"], "SCAN4")
    assert [r["bin_code"] for r in await candidates(flow)] == [OTHER[:-2], raw[:-2]]
    current = (await rows(flow, "return_batch"))[0]
    async with flow.factory() as session:
        position = await flow.store.repo.current_position(session, "BIN", case["bin_code"])
        assert position["source_id"] == new4["id"] and position["location_code"] == "SCAN4"
    for field in (
        "members",
        "operation_id",
        "message_id",
        "transport_task_id",
        "client_request_id",
        "transport_operation_id",
    ):
        assert current[field] == batch[field]
    async with flow.factory() as session:
        assert (
            await session.scalar(
                select(ReliableMessage.request_raw).where(ReliableMessage.id == batch["message_id"])
            )
            == frozen_request
        )
        assert (
            await session.scalar(
                select(ReliableMessage.request_raw).where(
                    ReliableMessage.operation_id == batch["transport_operation_id"]
                )
            )
            == frozen_transport
        )
    if old_transport == "WAITING_WMS":
        assert not current["pending"]
        assert (await rows(flow, "return_opportunity"))[0]["ended"]
        await flow.finish_departure(entry["cycle_id"])
        demand, _ = await flow.ready_supply(rack="R2", count=1)
        await flow.feed_success((await rows(flow, "feed_segment"))[0])
        await flow.advance("FEED", demand)
        new_batch = (await rows(flow, "return_batch"))[-1]
        assert [m["round_id"] for m in new_batch["members"]] == [
            other4["return_round_id"],
            new4["return_round_id"],
        ]
        await flow.complete_return()
        assert await candidates(flow) == ()
        assert dict((await rows(flow, "return_round"))[0]) == old_round


@pytest.mark.parametrize("opportunity_kind", ["FACE_END", "PURE_RETURN", "SEGMENT"])
@pytest.mark.parametrize("allocated_count", [1, 2])
async def test_recorded_old_allocation_releases_unchanged_members_in_fifo(
    roller_lifecycle, allocated_count, opportunity_kind
):
    flow = roller_lifecycle
    raw = BAD["scan3_reentry"]["raw"]
    old4 = await visit(flow, raw, 10)
    other4 = await visit(flow, OTHER, 20)
    for original in (old4, other4):
        await flow.result(original["command_code"], "SCAN4")
    entry = await return_opportunity(flow, opportunity_kind)
    batch = dict((await rows(flow, "return_batch"))[0])
    old_round = dict((await rows(flow, "return_round"))[0])
    new4 = await visit(flow, raw, 30)
    async with flow.factory.begin() as session:
        await flow.decide(session, batch["id"], count=allocated_count)
    await rebuild(flow)
    for _ in range(2):
        await flow.advance("RETURN_BATCH", batch["id"])
    current = (await rows(flow, "return_batch"))[0]
    assert not current["pending"] and current["transport_task_id"] is None
    assert current["members"] == batch["members"]
    assert dict((await rows(flow, "return_round"))[0]) == old_round
    if opportunity_kind == "SEGMENT":
        assert (await rows(flow, "return_opportunity"))[0]["end_reason"] == "SUPERSEDED_ROUNDS"
        assert [r["id"] for r in await candidates(flow)] == [other4["return_round_id"]]
        assert len(await rows(flow, "feed_segment")) == 2
        await flow.result(new4["command_code"], "SCAN4")
        await flow.feed_success((await rows(flow, "feed_segment"))[-1])
        await flow.advance("FEED", (await rows(flow, "supply_demand"))[0]["id"])
        current_batch = (await rows(flow, "return_batch"))[-1]
        assert [m["round_id"] for m in current_batch["members"]] == [
            other4["return_round_id"],
            new4["return_round_id"],
        ]
        await flow.complete_return(count=2)
        await flow.finish_departure(entry["cycle_id"])
        return
    batches = await rows(flow, "return_batch")
    assert len(batches) == 2
    continuation = batches[-1]
    assert continuation["source_ref"] == batch["id"]
    assert [m["round_id"] for m in continuation["members"]] == [other4["return_round_id"]]
    assert not (await rows(flow, "return_opportunity"))[0]["ended"]
    assert not await rows(flow, "departure")
    await flow.result(new4["command_code"], "SCAN4")
    await flow.complete_return(count=1)
    next_batch = (await rows(flow, "return_batch"))[-1]
    assert [m["round_id"] for m in next_batch["members"]] == [new4["return_round_id"]]
    await flow.complete_return(count=1)
    await flow.finish_departure(entry["cycle_id"])
    assert dict((await rows(flow, "return_round"))[0]) == old_round


@pytest.mark.parametrize("decision", ["READY", "NO_BATCH"])
async def test_recorded_allocation_tail_preserves_superseded_round(roller_lifecycle, decision):
    flow = roller_lifecycle
    raw = BAD["scan3_reentry"]["raw"]
    other4 = await visit(flow, OTHER, 10)
    old4 = await visit(flow, raw, 20)
    old3 = await arrival(flow, "SCAN3", raw)
    for original in (other4, old3, old4):
        await flow.result(original["command_code"], original["point"])
    await return_opportunity(flow)
    batch = dict((await rows(flow, "return_batch"))[0])
    async with flow.factory() as session:
        old_round = dict(
            await flow.roller.original_record(session, "return_round", old4["return_round_id"])
        )
        frozen_request = await session.scalar(
            select(ReliableMessage.request_raw).where(ReliableMessage.id == batch["message_id"])
        )
    assert [m["round_id"] for m in batch["members"]] == [
        other4["return_round_id"],
        old4["return_round_id"],
    ]
    # Each original point's applied result separates the actual new A visit.
    new4 = await visit(flow, raw, 30)
    assert new4["return_round_id"] != old4["return_round_id"]
    async with flow.factory.begin() as session:
        manifest = await flow.decide(
            session,
            batch["id"],
            count=1,
            data=dict(result="NO_BATCH") if decision == "NO_BATCH" else None,
        )
    await rebuild(flow)
    await flow.advance("RETURN_BATCH", batch["id"])
    applied = dict((await rows(flow, "return_batch"))[0])
    await flow.advance("RETURN_BATCH", batch["id"])
    async with flow.factory() as session:
        assert (
            dict(await flow.roller.original_record(session, "return_round", old_round["id"]))
            == old_round
        )
        current = await flow.roller.original_record(session, "return_batch", batch["id"])
        assert current["manifest"] == manifest
        for key in ("members", "message_id", "operation_id"):
            assert current[key] == batch[key]
        for key in ("transport_task_id", "transport_operation_id", "client_request_id"):
            assert current[key] == applied[key]
        assert (
            await session.scalar(
                select(ReliableMessage.request_raw).where(ReliableMessage.id == batch["message_id"])
            )
            == frozen_request
        )
    if decision == "NO_BATCH":
        assert applied["transport_task_id"] is None
        assert [r["id"] for r in await candidates(flow)] == [other4["return_round_id"]]
    else:
        assert applied["transport_task_id"] is not None
        assert await candidates(flow) == ()
        await flow.success(batch["id"])
        await flow.advance("RETURN_BATCH", batch["id"])
        async with flow.factory() as session:
            assert (
                await flow.roller.original_record(
                    session, "return_round", other4["return_round_id"]
                )
            )["status"] == "COMPLETED"
            assert (
                dict(await flow.roller.original_record(session, "return_round", old_round["id"]))
                == old_round
            )
    await flow.result(new4["command_code"], "SCAN4")
    assert [r["id"] for r in await candidates(flow)] == (
        [other4["return_round_id"], new4["return_round_id"]]
        if decision == "NO_BATCH"
        else [new4["return_round_id"]]
    )
