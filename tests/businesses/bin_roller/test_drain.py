import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef


@pytest.mark.parametrize("rack_count", [1, 2])
async def test_original_drain_release_commits_before_unconfigured_next_supply(
    roller_lifecycle, rack_count
):
    from app.models import WorkLine

    flow = roller_lifecycle
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
            racks=[dict(rack_id=f"D{i}", rack_face=["A"]) for i in range(1, rack_count + 1)],
        ),
    )
    await flow.advance("DRAIN", drain["id"])
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.complete_return(no_batch=True)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    action = (await flow.all_rows("rack_action"))[-1]
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.update().where(WorkLine.id == 1).values(position_bindings={})
        )
    await flow.accepted(action)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    cycles = await flow.all_rows("rack_cycle")
    assert [(c["id"], c["status"]) for c in cycles] == [(entry["cycle_id"], "RELEASED")]
    assert cycles[0]["release_basis"]["transport_task_id"] == action["transport_task_id"]
    assert [a["id"] for a in await flow.all_rows("rack_action")] == [entry["id"], action["id"]]
    assert [d["operation_id"] for d in await flow.all_rows("drain")] == [drain["operation_id"]]
    assert (await flow.all_rows("drain"))[0]["status"] == (
        "COMPLETED" if rack_count == 1 else "READY"
    )
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.update()
            .where(WorkLine.id == 1)
            .values(position_bindings=dict(FIVE_RACK="RACK-WORK", INLET="INLET", OUTLET="OUTLET"))
        )
    await flow.advance("SCAN", scan["id"])
    if rack_count == 1:
        assert len(await flow.all_rows("drain")) == 2
        assert len(await flow.all_rows("rack_cycle")) == 1
    else:
        assert len(await flow.all_rows("drain")) == 1
        assert [(c["rack_id"], c["status"]) for c in await flow.all_rows("rack_cycle")] == [
            ("D1", "RELEASED"),
            ("D2", "ACTIVE"),
        ]


async def test_scan4_prefers_original_supply_demand_before_drain(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.demand()
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    assert not await flow.all_rows("drain")
    assert [c["purpose"] for c in await flow.all_rows("rack_cycle")] == ["ORDINARY"]


@pytest.mark.parametrize("wait", [False, True])
async def test_ended_ordinary_departure_does_not_block_new_drain(roller_lifecycle, wait):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    departure = (await flow.all_rows("departure"))[0]
    if wait:
        await flow.decision("departure", departure["id"], dict(result="WAIT", retry_after_ms=60000))
        await flow.advance("RACK_CYCLE", cycle)
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    assert len(await flow.all_rows("drain")) == 1
    assert (await flow.all_rows("departure"))[0]["operation_id"] == departure["operation_id"]
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "ACTIVE"


@pytest.mark.parametrize("decision", ["WAIT", "READY"])
async def test_original_supply_request_survives_head_change_and_restart(roller_lifecycle, decision):
    from sqlalchemy import update

    from app.businesses._components.bin_roller.service import BinRoller

    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(2)
    scans = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"]
    await flow.advance("SCAN", scans[0]["id"])
    original = (await flow.all_rows("drain"))[0]
    data = (
        dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000)
        if decision == "WAIT"
        else dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.decision("drain", original["id"], data)
    await flow.advance("DRAIN", original["id"])
    rounds = await flow.all_rows("return_round")
    async with flow.factory.begin() as session:
        await session.execute(
            update(flow.store.tables["return_round"])
            .where(flow.store.tables["return_round"].c.id == rounds[0]["id"])
            .values(removed=True)
        )
    old = flow.roller
    flow.roller = BinRoller(old.context, old.capabilities, old.production, tables=old.tables)
    await flow.advance("SCAN", scans[1]["id"])
    current = (await flow.all_rows("drain"))[0]
    assert len(await flow.all_rows("drain")) == 1
    assert (
        current["id"],
        current["operation_id"],
        current["request"],
        current["input_snapshot"],
    ) == (original["id"], original["operation_id"], original["request"], original["input_snapshot"])


async def test_expired_wait_registers_one_new_original_evaluation(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    original = (await flow.all_rows("drain"))[0]
    await flow.decision(
        "drain",
        original["id"],
        dict(result="WAIT", reason_code="NO_DRAIN_RACK_AVAILABLE", retry_after_ms=60000),
    )
    await flow.advance("DRAIN", original["id"])
    async with flow.factory.begin() as session:
        table = flow.store.tables["drain"]
        await session.execute(table.update().where(table.c.id == original["id"]).values(due_at=0))
    await flow.advance("SCAN", scan["id"])
    await flow.advance("DRAIN", original["id"])
    drains = await flow.all_rows("drain")
    assert len(drains) == 2
    assert drains[1]["input_ref"] == original["id"]
    assert drains[1]["operation_id"] != original["operation_id"]


async def test_first_ready_fifo_requests_drain_without_existing_cycle(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(6)
    scans = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"]
    await flow.advance("SCAN", scans[0]["id"])
    drains = await flow.all_rows("drain")
    assert len(drains) == 1
    assert drains[0]["request"] == dict(workline_code="LINE-1", required_slot_count=6)
    assert not await flow.all_rows("rack_cycle")
    await flow.advance("SCAN", scans[1]["id"])
    assert [r["id"] for r in await flow.all_rows("drain")] == [drains[0]["id"]]


async def test_successor_becoming_none_rechecks_original_unassigned_fifo(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    assert not await flow.all_rows("drain")
    async with flow.factory() as session:
        upper = await flow.roller.get_pending_upper_id(session, kind="SCAN")
        work = await flow.roller.list_pending(
            session, kind="SCAN", after_id=None, upper_id=upper, limit=20
        )
    assert RollerWorkRef("SCAN", scan["id"]) in work
    flow.successor = "NONE"
    await flow.advance("SCAN", scan["id"])
    assert len(await flow.all_rows("drain")) == 1


async def test_two_rack_drain_graph_uses_first_batch_before_new_supply(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(6)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    drain = (await flow.all_rows("drain"))[0]
    await flow.decision(
        "drain",
        drain["id"],
        dict(
            result="READY",
            racks=[
                dict(rack_id="D1", rack_face=["A", "B"]),
                dict(rack_id="D2", rack_face=["A", "B"]),
            ],
        ),
    )
    await flow.advance("DRAIN", drain["id"])
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01"]
    first_cycle = actions[0]["cycle_id"]
    await flow.rack_result(actions[0])
    await flow.advance("RACK_CYCLE", first_cycle)
    await flow.complete_return(count=2)
    await flow.complete_return(no_batch=True)
    rotate = (await flow.all_rows("rack_action"))[-1]
    assert rotate["kind"] == "CTU02"
    await flow.rack_result(rotate)
    await flow.advance("RACK_CYCLE", first_cycle)
    await flow.complete_return(count=2)
    await flow.complete_return(no_batch=True)
    await flow.finish_departure(first_cycle)
    second_entry = (await flow.all_rows("rack_action"))[-1]
    assert second_entry["kind"] == "CTU01" and second_entry["cycle_id"] != first_cycle
    assert len(await flow.all_rows("drain")) == 1
    await flow.rack_result(second_entry)
    await flow.advance("RACK_CYCLE", second_entry["cycle_id"])
    await flow.complete_return(count=2)
    await flow.finish_departure(second_entry["cycle_id"])
    assert (await flow.all_rows("drain"))[0]["status"] == "COMPLETED"
    assert [a["kind"] for a in await flow.all_rows("rack_action")].count("CTU02") == 1
    assert len(await flow.all_rows("return_batch")) == 5
    assert not await flow.all_rows("feed")


async def test_empty_buffer_keeps_registered_arrival_and_unused_candidates(roller_lifecycle):
    flow = roller_lifecycle
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
            racks=[dict(rack_id="D1", rack_face=["A", "B"]), dict(rack_id="D2", rack_face=["A"])],
        ),
    )
    await flow.advance("DRAIN", drain["id"])
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory.begin() as session:
        table = flow.store.tables["return_round"]
        await session.execute(table.update().values(removed=True))
    await flow.advance("DRAIN", drain["id"])
    drain = (await flow.all_rows("drain"))[0]
    assert drain["status"] == "READY" and drain["unused_racks"] == ["D2"]
    assert not await flow.all_rows("departure")
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert not await flow.all_rows("return_batch")
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]
    await flow.finish_departure(entry["cycle_id"])
    assert (await flow.all_rows("drain"))[0]["status"] == "COMPLETED"
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]


async def test_completed_supply_history_allows_new_demand(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    first = (await flow.all_rows("drain"))[0]
    async with flow.factory.begin() as session:
        table = flow.store.tables["return_round"]
        await session.execute(table.update().values(removed=True))
    await flow.decision(
        "drain", first["id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", first["id"])
    assert (await flow.all_rows("drain"))[0]["status"] == "COMPLETED"
    assert not await flow.all_rows("rack_action")
    await flow.bins(1, offset=5)
    later = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"][-1]
    await flow.advance("SCAN", later["id"])
    await flow.advance("DRAIN", first["id"])
    drains = await flow.all_rows("drain")
    assert len(drains) == 2
    assert drains[1]["input_kind"] == "COMPLETED"
    assert drains[1]["input_ref"] == first["id"]


async def test_completed_or_removed_round_is_not_pending_scan_work(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(2)
    scans = [a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4"]
    async with flow.factory.begin() as session:
        rounds = flow.store.tables["return_round"]
        await session.execute(
            rounds.update()
            .where(rounds.c.id == scans[0]["return_round_id"])
            .values(status="COMPLETED")
        )
        await session.execute(
            rounds.update().where(rounds.c.id == scans[1]["return_round_id"]).values(removed=True)
        )
        upper = await flow.roller.get_pending_upper_id(session, kind="SCAN")
        pending = await flow.roller.list_pending(
            session, kind="SCAN", after_id=None, upper_id=upper, limit=20
        )
        assert not {s["id"] for s in scans} & {p.record_id for p in pending}


async def test_empty_box_flow_does_not_wait_for_another_original_arrival(roller_lifecycle):
    from app.core.identifiers import uuid7

    flow = roller_lifecycle
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
            racks=[
                dict(rack_id="D1", rack_face=["A", "B"]),
                dict(rack_id="D2", rack_face=["A", "B"]),
            ],
        ),
    )
    await flow.advance("DRAIN", drain["id"])
    first = (await flow.all_rows("rack_cycle"))[0]
    # An already selected original candidate keeps its independent arrival obligation.
    async with flow.factory.begin() as session:
        second = await flow.roller.repository(1).create_cycle(
            session,
            cycle_id=str(uuid7()),
            rack_id="D2",
            purpose="DRAIN",
            task_id=None,
            source_ref=drain["id"],
            resources=first["resources"],
        )
    await flow.advance("RACK_CYCLE", second["id"])
    entries = await flow.all_rows("rack_action")
    await flow.rack_result(entries[0])
    await flow.advance("RACK_CYCLE", first["id"])
    await flow.complete_return(count=1)
    assert [d["cycle_id"] for d in await flow.all_rows("departure")] == [first["id"]]
    await flow.finish_departure(first["id"])
    assert (await flow.all_rows("drain"))[0]["status"] == "READY"
    await flow.rack_result(entries[1])
    await flow.advance("RACK_CYCLE", second["id"])
    await flow.finish_departure(second["id"])
    assert (await flow.all_rows("drain"))[0]["status"] == "COMPLETED"
    assert [a["kind"] for a in await flow.all_rows("rack_action")].count("CTU02") == 0


async def test_new_task_does_not_convert_original_drain_purpose(roller_lifecycle):
    flow = roller_lifecycle
    flow.successor = "NONE"
    await flow.bins(1)
    scan = next(a for a in await flow.all_rows("scan_arrival") if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    drain = (await flow.all_rows("drain"))[0]
    await flow.decision(
        "drain", drain["id"], dict(result="READY", racks=[dict(rack_id="R1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", drain["id"])
    original = (await flow.all_rows("rack_action"))[0]
    flow.current_task, flow.successor = "T2", "UNKNOWN"
    demand = await flow.demand(task="T2")
    await flow.advance("FEED", demand)
    assert {c["purpose"] for c in await flow.all_rows("rack_cycle")} == {"DRAIN", "ORDINARY"}
    await flow.rack_result(original)
    await flow.advance("RACK_CYCLE", original["cycle_id"])
    assert len(await flow.all_rows("return_batch")) == 1
    assert not await flow.all_rows("feed")
    assert (
        next(c for c in await flow.all_rows("rack_cycle") if c["id"] == original["cycle_id"])[
            "purpose"
        ]
        == "DRAIN"
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
