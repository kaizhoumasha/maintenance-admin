import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef, SourceFaceRef, SupplyChange


async def test_unselected_demand_prepares_original_entry_in_same_transaction(roller_lifecycle):
    flow = roller_lifecycle
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session, SupplyChange(1, "task-original", "M1", (SourceFaceRef(1, "R1", "A"),), ())
        )
        demand = (await flow.rows(session, "supply_demand"))[0]
        await flow.roller.advance(session, RollerWorkRef("FACE", demand["id"]))
        actions = await flow.rows(session, "rack_action")
        assert [a["kind"] for a in actions] == ["CTU01"]


async def test_first_arrival_opens_feed_without_rotation(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]
    assert len(await flow.all_rows("feed")) == 1
    await flow.advance("FACE", demand)
    assert len(await flow.all_rows("feed")) == 1


async def test_final_face_done_prepares_departure_without_fake_feed(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", demand)
    assert not await flow.all_rows("feed_segment")
    departure = (await flow.all_rows("departure"))[0]
    assert departure["request"] == dict(
        workline_code="LINE-1",
        rack_id="R1",
        current_location=dict(type="RACK_POSITION", location_code="RACK-WORK"),
        current_face="A",
    )
    import json

    from app.models import ReliableMessage

    async with flow.factory() as session:
        message = await session.get(ReliableMessage, departure["message_id"])
        assert (message.operation, message.operation_id, message.business_key) == (
            "rack.departure_decide@v1",
            departure["operation_id"],
            "business:first",
        )
        timestamp = json.loads(message.request_raw)["timestamp"]
        assert (
            message.request_raw
            == json.dumps(
                dict(
                    operation_id=departure["operation_id"],
                    operation="rack.departure_decide@v1",
                    timestamp=timestamp,
                    data=departure["request"],
                ),
                separators=(",", ":"),
            ).encode()
        )
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("RACK_CYCLE", (await flow.all_rows("rack_cycle"))[0]["id"])
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01", "CTU03"]
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "ACTIVE"
    await flow.accepted(actions[-1])
    await flow.advance("RACK_CYCLE", actions[-1]["cycle_id"])
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"


async def test_original_cycle_survives_configuration_change(roller_lifecycle):
    from app.models import WorkLine

    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.update()
            .where(WorkLine.id == 1)
            .values(position_bindings={}, line_code="CHANGED")
        )
    await flow.rack_result(entry)
    await flow.advance("FACE", demand)
    feed = (await flow.all_rows("feed"))[0]
    assert feed["target"]["location_code"] == "INLET"
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", demand)
    assert (await flow.all_rows("departure"))[0]["request"]["workline_code"] == "LINE-1"


@pytest.mark.parametrize("unavailable", ["bindings", "ownership", "missing_line"])
async def test_original_release_commits_when_queued_supply_configuration_is_unavailable(
    roller_lifecycle, unavailable
):
    from app.models import WorkLine

    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", original)
    queued = await flow.demand(revision=2)
    await flow.advance("FACE", queued)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("RACK_CYCLE", cycle)
    original_actions = await flow.all_rows("rack_action")
    action = original_actions[-1]
    async with flow.factory.begin() as session:
        line = WorkLine.__table__
        if unavailable == "missing_line":
            from app.models import WorkLinePosition

            await session.execute(
                WorkLinePosition.__table__.delete().where(WorkLinePosition.work_line_id == 1)
            )
            await session.execute(line.delete().where(line.c.id == 1))
        else:
            values = (
                dict(position_bindings={})
                if unavailable == "bindings"
                else dict(business_code="second")
            )
            await session.execute(line.update().where(line.c.id == 1).values(**values))
    await flow.accepted(action)
    await flow.advance("RACK_CYCLE", cycle)
    cycles = await flow.all_rows("rack_cycle")
    assert [(c["id"], c["status"]) for c in cycles] == [(cycle, "RELEASED")]
    assert cycles[0]["release_basis"]["transport_task_id"] == action["transport_task_id"]
    assert [
        (a["id"], a["operation_id"], a["transport_task_id"], a["client_request_id"])
        for a in await flow.all_rows("rack_action")
    ] == [
        (a["id"], a["operation_id"], a["transport_task_id"], a["client_request_id"])
        for a in original_actions
    ]
    assert len(await flow.all_rows("feed")) == len(await flow.all_rows("departure")) == 1
    assert all(not s["pending"] for s in await flow.all_rows("face_service"))
    assert (await flow.all_rows("departure"))[0]["operation_id"] == departure["operation_id"]
    async with flow.factory() as session:
        upper = await flow.roller.get_pending_upper_id(session, kind="FACE")
        pending = await flow.roller.list_pending(
            session, kind="FACE", after_id=None, upper_id=upper, limit=10
        )
    assert RollerWorkRef("FACE", queued) in pending
    assert (
        next(d for d in await flow.all_rows("supply_demand") if d["id"] == queued)["cycle_id"]
        is None
    )
    async with flow.factory.begin() as session:
        values = dict(
            line_code="LINE-1",
            line_name="Test",
            business_code="first",
            position_bindings=dict(FIVE_RACK="RACK-WORK", INLET="INLET", OUTLET="OUTLET"),
        )
        if unavailable == "missing_line":
            await session.execute(line.insert().values(id=1, **values))
            from tests.helpers import configure_rack_positions

            await configure_rack_positions(session, await session.get(WorkLine, 1))
        else:
            await session.execute(line.update().where(line.c.id == 1).values(**values))
    await flow.advance("FACE", queued)
    demands = await flow.all_rows("supply_demand")
    next_cycle = next(d for d in demands if d["id"] == queued)["cycle_id"]
    assert next_cycle is not None and next_cycle != cycle
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03", "CTU01"]


@pytest.mark.parametrize("face", ["A", "B"])
async def test_same_task_new_revision_waits_for_original_service_then_reuses_cycle(
    roller_lifecycle, face
):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    new = await flow.demand(face=face, revision=2)
    await flow.advance("FACE", new)
    assert len(await flow.all_rows("feed")) == 1
    segment = (await flow.all_rows("feed_segment"))[0]
    await flow.feed_success(segment)
    await flow.advance("FACE", demand)
    actions = await flow.all_rows("rack_action")
    if face == "B":
        assert [a["kind"] for a in actions] == ["CTU01", "CTU02"]
        await flow.rack_result(actions[-1])
        await flow.advance("FACE", new)
    else:
        assert [a["kind"] for a in actions] == ["CTU01"]
    assert len(await flow.all_rows("face_service")) == 2
    assert len(await flow.all_rows("feed")) == 2
    assert len(await flow.all_rows("rack_cycle")) == 1
    assert not await flow.all_rows("departure")


@pytest.mark.parametrize("wait", [False, True])
async def test_new_revision_does_not_preempt_registered_departure(roller_lifecycle, wait):
    flow = roller_lifecycle
    old, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", old)
    departure = (await flow.all_rows("departure"))[0]
    if wait:
        await flow.decision("departure", departure["id"], dict(result="WAIT", retry_after_ms=60000))
        await flow.advance("RACK_CYCLE", cycle)
    new = await flow.demand(revision=2)
    await flow.advance("FACE", new)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1
    assert not next(d for d in await flow.all_rows("supply_demand") if d["id"] == new)["service_id"]


async def test_new_task_does_not_adopt_original_cycle(roller_lifecycle):
    flow = roller_lifecycle
    old, cycle = await flow.ready_supply(count=1)
    flow.current_task = "T2"
    new = await flow.demand(task="T2")
    await flow.advance("FACE", new)
    cycles = await flow.all_rows("rack_cycle")
    assert len(cycles) == 2
    assert {c["task_id"] for c in cycles} == {"task-original", "T2"}
    assert len(await flow.all_rows("rack_action")) == 1
    assert next(c for c in cycles if c["task_id"] == "T2")["id"] != cycle


@pytest.mark.parametrize(
    "http,code,released",
    [
        (202, "RECEIVED", True),
        (200, "DUPLICATE", True),
        (200, "RECEIVED", False),
        (503, "UNAVAILABLE", False),
    ],
)
async def test_only_valid_original_departure_acceptance_releases(
    roller_lifecycle, http, code, released
):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="RACK_POSITION", location_code="DEST")),
    )
    await flow.advance("RACK_CYCLE", cycle)
    action = (await flow.all_rows("rack_action"))[-1]
    await flow.accepted(action, code=code, http=http)
    await flow.advance("RACK_CYCLE", cycle)
    assert ((await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED") is released
    assert len(await flow.all_rows("rack_action")) == 2
    if released:
        await flow.rack_result(action, status="FAILED")
        await flow.advance("RACK_CYCLE", cycle)
        assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"


async def test_replayed_departure_acceptance_keeps_new_rack(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    await flow.finish_departure(cycle)
    new = await flow.demand(rack="R2")
    await flow.advance("FACE", new)
    entry = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.advance("RACK_CYCLE", cycle)
    positions = [
        p
        for p in await flow.all_rows("rack_position")
        if p["object_kind"] == "RACK" and p["is_current"]
    ]
    assert [(p["object_code"], p["location_code"]) for p in positions] == [("R2", "RACK-WORK")]


async def test_withdrawn_queued_revision_does_not_inherit_old_entry_obligation(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    new = await flow.demand(revision=2)
    await flow.advance("FACE", new)
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1, flow.current_task, "withdraw-queued", (), (SourceFaceRef(2, "R1", "A"),)
            ),
        )
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", original)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1
    queued = next(d for d in await flow.all_rows("supply_demand") if d["id"] == new)
    assert queued["close_reason"] == "WITHDRAWN" and queued["preparation_action_id"] is None


async def test_task_switch_does_not_start_queued_old_supply(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    new = await flow.demand(revision=2)
    await flow.advance("FACE", new)
    flow.current_task = "T2"
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", original)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1


async def test_old_task_settlement_does_not_admit_or_borrow_new_task_entry(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    old_segment = (await flow.all_rows("feed_segment"))[0]
    old_entries = await flow.all_rows("rack_action")
    flow.current_task = "T2"
    new = await flow.demand(task="T2")
    await flow.advance("FACE", new)
    assert [a["id"] for a in await flow.all_rows("rack_action")] == [a["id"] for a in old_entries]
    await flow.feed_success(old_segment)
    await flow.advance("FACE", original)
    assert [d["cycle_id"] for d in await flow.all_rows("departure")] == [cycle]
    assert len(await flow.all_rows("feed")) == 1
    assert not [a for a in await flow.all_rows("rack_action") if a["cycle_id"] != cycle]


async def test_departure_wait_keeps_due_point_then_registers_one_recheck(
    roller_lifecycle, monkeypatch
):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    original = (await flow.all_rows("departure"))[0]
    await flow.decision("departure", original["id"], dict(result="WAIT", retry_after_ms=60000))
    await flow.advance("RACK_CYCLE", cycle)
    due = (await flow.all_rows("departure"))[0]["due_at"]
    await flow.advance("RACK_CYCLE", cycle)
    assert (await flow.all_rows("departure"))[0]["due_at"] == due
    monkeypatch.setattr(
        "app.businesses._components.bin_roller.rack_flow.time", lambda: (due + 1) / 1000
    )
    await flow.advance("RACK_CYCLE", cycle)
    await flow.advance("FACE", demand)
    requests = await flow.all_rows("departure")
    assert len(requests) == 2
    assert requests[1]["previous_action_id"] == original["id"]
    assert len(await flow.all_rows("rack_action")) == 1


@pytest.mark.parametrize(
    "data",
    [
        dict(
            result="READY", rack_destination=dict(type="RACK_POSITION", location_code="RACK-WORK")
        ),
        dict(result="WAIT", retry_after_ms=0),
        dict(
            result="READY",
            rack_destination=dict(type="ZONE", location_code="DEST"),
            task_id="extra",
        ),
    ],
)
async def test_invalid_departure_never_creates_move_or_new_identity(roller_lifecycle, data):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    original = (await flow.all_rows("departure"))[0]
    await flow.decision("departure", original["id"], data)
    await flow.advance("FACE", demand)
    await flow.advance("RACK_CYCLE", cycle)
    assert [d["operation_id"] for d in await flow.all_rows("departure")] == [
        original["operation_id"]
    ]
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]


@pytest.mark.parametrize("kind", ["CTU01", "CTU02"])
@pytest.mark.parametrize("mismatch", ["target", "face"])
@pytest.mark.parametrize("consume_observation", [False, True])
async def test_newer_mismatch_blocks_continuation_after_older_success(
    roller_lifecycle, kind, mismatch, consume_observation
):
    from app.integrations.wms_transport import TransportCaller
    from app.models import WorkLine

    flow = roller_lifecycle
    if kind == "CTU01":
        demand = await flow.demand()
        await flow.advance("FACE", demand)
    else:
        original, _ = await flow.ready_supply(count=1)
        demand = await flow.demand(face="B", revision=2)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FACE", original)
    originals = await flow.all_rows("rack_action")
    action = originals[-1]
    assert action["kind"] == kind
    feeds = await flow.all_rows("feed")
    location = "OTHER" if mismatch == "target" else "RACK-WORK"
    face = action["target_face"] if mismatch == "target" else "UNEXPECTED"
    await flow.rack_callback(
        dict(
            transport_task_id=action["transport_task_id"],
            rack_id="R1",
            kind="RACK_ROTATE" if kind == "CTU02" else "RACK_MOVE",
            outcome_revision=3,
            status="SUCCEEDED",
            final_position=dict(kind="RACK_POSITION", location_code=location),
            arrival_face=face,
        )
    )
    if consume_observation:
        await flow.advance("RACK_CYCLE", action["cycle_id"])
    await flow.rack_result(action, revision=2)
    async with flow.factory.begin() as session:
        view = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=TransportCaller(action["caller_workline_id"], action["station_id"]),
        )
        assert view.outcome_revision == 2 and view.outcome["status"] == "SUCCEEDED"
        assert view.position_observation["outcome_revision"] == 3
        await session.execute(
            WorkLine.__table__.update()
            .where(WorkLine.id == 1)
            .values(position_bindings=dict(FIVE_RACK=location, INLET="CHANGED", OUTLET="CHANGED"))
        )
    progress = await flow.advance("RACK_CYCLE", action["cycle_id"])
    await flow.advance("FACE", demand)
    actions = await flow.all_rows("rack_action")
    assert [
        (a["id"], a["transport_task_id"], a["client_request_id"], a["operation_id"])
        for a in actions
    ] == [
        (a["id"], a["transport_task_id"], a["client_request_id"], a["operation_id"])
        for a in originals
    ]
    assert await flow.all_rows("feed") == feeds
    assert actions[-1]["outcome"]["status"] == "SUCCEEDED"
    assert actions[-1]["phase"] == "REVIEW"
    assert (actions[-1]["consumed_revision"], actions[-1]["position_revision"]) == (2, 3)
    current = [p for p in await flow.all_rows("rack_position") if p["is_current"]]
    assert [
        (p["location_code"], p["rack_face"], p["source_id"], p["source_revision"]) for p in current
    ] == [(location, face, action["id"], 3)]
    assert (await flow.all_rows("rack_cycle"))[0]["pending"]
    assert progress in {"WAITING_FOR_ORIGINAL_POSITION", "WAITING_FOR_ORIGINAL_RESULT"}


async def test_departure_result_without_original_submit_acceptance_keeps_capacity(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    decision = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        decision["id"],
        dict(
            result="READY", rack_destination=dict(type="RACK_POSITION", location_code="STORAGE-P1")
        ),
    )
    await flow.advance("RACK_CYCLE", cycle)
    departure = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(departure)
    await flow.advance("RACK_CYCLE", cycle)
    assert (await flow.all_rows("rack_cycle"))[0]["pending"]


async def test_face_end_waits_for_original_scan_round_before_handoff(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.scan("SCAN3", "A000000008-B", timestamp=10)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", demand)
    assert not await flow.all_rows("departure")
    await flow.scan("SCAN4", "A000000008-B", timestamp=11)
    arrival = next(r for r in await flow.all_rows("scan_arrival") if r["point"] == "SCAN4")
    await flow.result(arrival["command_code"], "SCAN4")
    await flow.advance("RETURN", arrival["round_id"])
    await flow.advance("RACK_CYCLE", cycle)
    assert len(await flow.all_rows("return_batch")) == 1
    await flow.complete_return(no_batch=True)
    assert len(await flow.all_rows("departure")) == 1


@pytest.mark.parametrize("rotate", [False, True])
async def test_submit_ack_alone_never_supplies_another_face(roller_lifecycle, rotate):
    flow = roller_lifecycle
    if rotate:
        first, _ = await flow.ready_supply(count=1)
        await flow.demand(face="B", revision=2)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FACE", first)
    else:
        first = await flow.demand()
        await flow.advance("FACE", first)
    action = (await flow.all_rows("rack_action"))[-1]
    assert action["kind"] == ("CTU02" if rotate else "CTU01")
    existing = await flow.all_rows("feed")
    await flow.accepted(action)
    await flow.advance("RACK_CYCLE", action["cycle_id"])
    assert await flow.all_rows("feed") == existing
    await flow.rack_result(action)
    await flow.advance("RACK_CYCLE", action["cycle_id"])
    assert len(await flow.all_rows("feed")) == len(existing) + 1


@pytest.mark.parametrize("at_work_point", [False, True])
async def test_withdrawn_failed_entry_uses_observed_location_without_starting_feed(
    roller_lifecycle, at_work_point
):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(1, flow.current_task, "cancel-face", (), (SourceFaceRef(1, "R1", "A"),)),
        )
    await flow.rack_callback(
        dict(
            transport_task_id=entry["transport_task_id"],
            rack_id="R1",
            kind="RACK_MOVE",
            outcome_revision=1,
            status="FAILED",
            failure_code="RCS_EXECUTION_FAILED",
            final_position=dict(
                kind="RACK_POSITION", location_code="RACK-WORK" if at_work_point else "OTHER"
            ),
            arrival_face="A",
        )
    )
    for _ in range(3):
        await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert not await flow.all_rows("feed")
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.actions[0].outcome["status"] == "FAILED"
        assert job.state == ("RELEASING" if at_work_point else "NOT_ARRIVED")
        assert len([a for a in job.actions if a.kind == "DEPARTURE_DECIDE"]) == int(at_work_point)


async def test_missing_original_face_cannot_authorize_departure(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    # The task and another face exist, but cannot stand in for the missing original.
    await flow.demand(rack="OTHER", revision=2)
    async with flow.factory.begin() as session:
        faces = flow.store.tables["face_work"]
        await session.execute(faces.delete().where(faces.c.id == demand))
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert not await flow.all_rows("departure")
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=entry["cycle_id"])
        assert job.pending and len(job.actions) == 1 and not job.release_requested


async def test_original_face_authorization_cannot_be_replaced_on_result(roller_lifecycle):
    flow = roller_lifecycle
    first, cycle = await flow.ready_supply(count=1)
    second = await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", first)
    entry, rotation = await flow.all_rows("rack_action")
    await flow.rack_result(rotation)
    async with flow.factory.begin() as session:
        faces = flow.store.tables["face_work"]
        await session.execute(
            faces.update().where(faces.c.id == second).values(preparation_action_id=entry["id"])
        )
    await flow.advance("RACK_CYCLE", cycle)
    row = next(r for r in await flow.all_rows("face_work") if r["id"] == second)
    assert row["preparation_action_id"] == entry["id"] and row["feed_operation_id"] is None


async def awaiting_allocation(flow, rack):
    demand = await flow.demand(rack=rack)
    await flow.advance("FACE", demand)
    entry = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await flow.all_rows("feed"))[-1]
    return demand, entry, feed


@pytest.mark.parametrize("work", ["FACE", "RETURN"])
@pytest.mark.parametrize("invalid", ["replaced", "newer_observation"])
async def test_new_box_transport_requires_current_original_face(
    roller_lifecycle, monkeypatch, work, invalid
):
    from sqlalchemy import select

    from app.models import TransportTask

    flow = roller_lifecycle
    demand, entry, feed = await awaiting_allocation(flow, "R1")
    if work == "RETURN":
        await flow.bins(1)
        await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
        await flow.advance("FACE", demand)
        batch = (await flow.all_rows("return_batch"))[0]
    if invalid == "replaced":
        await awaiting_allocation(flow, "R2")
    else:
        # A recovered newer observation is independent of the lower applied original success.
        from dataclasses import replace

        port = flow.roller.capabilities.transport
        original = port.get_result

        async def observed(*args, **kwargs):
            result = await original(*args, **kwargs)
            if result.transport_task_id != entry["transport_task_id"]:
                return result
            return replace(
                result,
                position_observation=dict(
                    result.outcome,
                    outcome_revision=2,
                    final_position=dict(kind="RACK_POSITION", location_code="OTHER"),
                ),
            )

        monkeypatch.setattr(port, "get_result", observed)
    if work == "FACE":
        await flow.decision(
            "feed",
            feed["id"],
            dict(
                result="READY",
                bins=[
                    dict(
                        bin_code="A000000001",
                        source_locator=dict(
                            type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id="1"
                        ),
                    )
                ],
            ),
        )
        record = demand
    else:
        await flow.decision(
            "return_batch",
            batch["id"],
            dict(
                result="READY",
                moves=[
                    dict(
                        sequence_no=1,
                        bin_code=batch["members"][0]["bin_code"],
                        target=dict(type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id="1"),
                    )
                ],
            ),
        )
        record = batch["id"]
    await flow.advance("FACE", record)
    async with flow.factory() as session:
        assert not await session.scalar(
            select(TransportTask.transport_task_id).where(
                TransportTask.cycle_id == entry["cycle_id"], TransportTask.kind == "BIN_MOVE"
            )
        )
    assert not await flow.all_rows("feed_segment")


async def test_two_active_original_position_claims_do_not_choose_latest(roller_lifecycle):
    flow = roller_lifecycle
    first, entry, feed = await awaiting_allocation(flow, "R1")
    second = await flow.demand(rack="R2")
    await flow.advance("FACE", second)
    replacement = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(replacement)
    await flow.advance("RACK_CYCLE", replacement["cycle_id"])
    async with flow.factory() as session:
        assert (
            await flow.roller.rack_dispatch.get_point_preparations(
                session, work_line_id=1, point="RACK-WORK"
            )
            == ()
        )
    await flow.decision(
        "feed",
        feed["id"],
        dict(
            result="READY",
            bins=[
                dict(
                    bin_code="A000000001",
                    source_locator=dict(
                        type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id="S1"
                    ),
                )
            ],
        ),
    )
    await flow.advance("FACE", first)
    row = next(r for r in await flow.all_rows("face_work") if r["id"] == first)
    assert row["current_transport_task_id"] is None


@pytest.mark.parametrize("association", ["missing", "older_same_face"])
async def test_ready_face_cannot_grant_box_work_from_missing_or_old_authority(
    roller_lifecycle, association
):
    flow = roller_lifecycle
    first, job = await flow.ready_supply(count=1)
    second = await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", first)
    await flow.rack_result((await flow.all_rows("rack_action"))[-1])
    await flow.advance("FACE", second)
    third = await flow.demand(face="A", revision=3)
    feed = (await flow.all_rows("feed"))[-1]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", second)
    actions = await flow.all_rows("rack_action")
    assert [a["target_face"] for a in actions] == ["A", "B", "A"]
    await flow.rack_result(actions[-1])
    async with flow.factory.begin() as session:
        table = flow.store.tables["face_work"]
        await session.execute(
            table.update()
            .where(table.c.id == third)
            .values(
                preparation_action_id=actions[0]["id"]
                if association == "older_same_face"
                else None,
            )
        )
    await flow.advance("RACK_CYCLE", job)
    row = next(r for r in await flow.all_rows("face_work") if r["id"] == third)
    assert row["feed_operation_id"] is None and row["phase"] == "WAIT_RACK"


@pytest.mark.parametrize("work", ["FACE", "RETURN"])
async def test_inflight_box_result_is_consumed_after_rack_replacement(roller_lifecycle, work):
    from sqlalchemy import select

    from app.models import TransportTask

    flow = roller_lifecycle
    if work == "FACE":
        demand, cycle = await flow.ready_supply(count=5)
        original = (await flow.all_rows("feed_segment"))[0]
        record = demand
    else:
        demand, entry, feed = await awaiting_allocation(flow, "R1")
        cycle = entry["cycle_id"]
        await flow.bins(1)
        await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
        await flow.advance("FACE", demand)
        batch = (await flow.all_rows("return_batch"))[0]
        async with flow.factory.begin() as session:
            await flow.decide(session, batch["id"])
        await flow.advance("FACE", batch["id"])
        original = (await flow.all_rows("return_batch"))[0]
        record = batch["id"]
    assert original["transport_task_id"]
    await awaiting_allocation(flow, "R2")
    if work == "FACE":
        await flow.feed_success(original)
    else:
        await flow.success(record)
    await flow.advance("FACE", record)
    await flow.advance("FACE", record)
    current = next(r for r in await flow.all_rows("face_work") if r["id"] == record)
    if work == "FACE":
        assert current["feed_cursor"] == 4
    else:
        assert (await flow.all_rows("bin_return"))[0]["close_reason"] == "COMPLETED"
    async with flow.factory() as session:
        assert list(
            await session.scalars(
                select(TransportTask.transport_task_id).where(
                    TransportTask.cycle_id == cycle, TransportTask.kind == "BIN_MOVE"
                )
            )
        ) == [original["transport_task_id"]]
