import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef, SourceFaceRef, SupplyChange


async def test_unselected_demand_prepares_original_entry_in_same_transaction(roller_lifecycle):
    flow = roller_lifecycle
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session, SupplyChange(1, "task-original", "M1", (SourceFaceRef(1, "R1", "A"),), ())
        )
        demand = (await flow.rows(session, "supply_demand"))[0]
        await flow.roller.advance(session, RollerWorkRef("FEED", demand["id"]))
        actions = await flow.rows(session, "rack_action")
        assert [a["kind"] for a in actions] == ["CTU01"]


async def test_first_arrival_opens_feed_without_rotation(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]
    assert len(await flow.all_rows("feed")) == 1
    await flow.advance("FEED", demand)
    assert len(await flow.all_rows("feed")) == 1


async def test_final_face_done_prepares_departure_without_fake_feed(roller_lifecycle):
    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    assert not await flow.all_rows("feed_segment")
    departure = (await flow.all_rows("departure"))[0]
    assert departure["request"] == dict(
        workline_code="LINE-1",
        rack_id="R1",
        current_location=dict(type="RACK_POSITION", location_code="RACK-WORK"),
        current_face="A",
    )
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.advance("FEED", demand)
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01", "CTU03"]
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "ACTIVE"
    await flow.accepted(actions[-1])
    await flow.advance("FEED", demand)
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"


async def test_original_cycle_survives_configuration_change(roller_lifecycle):
    from app.models import WorkLine

    flow = roller_lifecycle
    demand = await flow.demand()
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.update()
            .where(WorkLine.id == 1)
            .values(position_bindings={}, line_code="CHANGED")
        )
    await flow.rack_result(entry)
    await flow.advance("FEED", demand)
    feed = (await flow.all_rows("feed"))[0]
    assert feed["target"]["location_code"] == "INLET"
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    assert (await flow.all_rows("departure"))[0]["request"]["workline_code"] == "LINE-1"


@pytest.mark.parametrize("unavailable", ["bindings", "ownership", "missing_line"])
async def test_original_release_commits_when_queued_supply_configuration_is_unavailable(
    roller_lifecycle, unavailable
):
    from app.models import WorkLine

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
    original_actions = await flow.all_rows("rack_action")
    action = original_actions[-1]
    async with flow.factory.begin() as session:
        line = WorkLine.__table__
        if unavailable == "missing_line":
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
        upper = await flow.roller.get_pending_upper_id(session, kind="FEED")
        pending = await flow.roller.list_pending(
            session, kind="FEED", after_id=None, upper_id=upper, limit=10
        )
    assert RollerWorkRef("FEED", queued) in pending
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
        else:
            await session.execute(line.update().where(line.c.id == 1).values(**values))
    await flow.advance("FEED", queued)
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
    await flow.advance("FEED", new)
    assert len(await flow.all_rows("feed")) == 1
    segment = (await flow.all_rows("feed_segment"))[0]
    await flow.feed_success(segment)
    await flow.advance("FEED", demand)
    actions = await flow.all_rows("rack_action")
    if face == "B":
        assert [a["kind"] for a in actions] == ["CTU01", "CTU02"]
        await flow.rack_result(actions[-1])
        await flow.advance("FEED", new)
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
    await flow.advance("FEED", old)
    departure = (await flow.all_rows("departure"))[0]
    if wait:
        await flow.decision("departure", departure["id"], dict(result="WAIT", retry_after_ms=60000))
        await flow.advance("RACK_CYCLE", cycle)
    new = await flow.demand(revision=2)
    await flow.advance("FEED", new)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1
    assert not next(d for d in await flow.all_rows("supply_demand") if d["id"] == new)["service_id"]


async def test_new_task_does_not_adopt_original_cycle(roller_lifecycle):
    flow = roller_lifecycle
    old, cycle = await flow.ready_supply(count=1)
    flow.current_task = "T2"
    new = await flow.demand(task="T2")
    await flow.advance("FEED", new)
    cycles = await flow.all_rows("rack_cycle")
    assert len(cycles) == 2
    assert {c["task_id"] for c in cycles} == {"task-original", "T2"}
    assert len(await flow.all_rows("rack_action")) == 2


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
    await flow.advance("FEED", demand)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="RACK_POSITION", location_code="DEST")),
    )
    await flow.advance("FEED", demand)
    action = (await flow.all_rows("rack_action"))[-1]
    await flow.accepted(action, code=code, http=http)
    await flow.advance("FEED", demand)
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
    await flow.advance("FEED", demand)
    await flow.finish_departure(cycle)
    new = await flow.demand(rack="R2")
    await flow.advance("FEED", new)
    entry = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.advance("RACK_CYCLE", cycle)
    positions = [
        p
        for p in await flow.all_rows("position_fact")
        if p["object_kind"] == "RACK" and p["is_current"]
    ]
    assert [(p["object_code"], p["location_code"]) for p in positions] == [("R2", "RACK-WORK")]


async def test_withdrawn_queued_revision_does_not_inherit_old_entry_obligation(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    new = await flow.demand(revision=2)
    await flow.advance("FEED", new)
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1, flow.current_task, "withdraw-queued", (), (SourceFaceRef(2, "R1", "A"),)
            ),
        )
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", original)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1
    queued = next(d for d in await flow.all_rows("supply_demand") if d["id"] == new)
    assert queued["cycle_id"] is None and queued["service_id"] is None


async def test_task_switch_does_not_start_queued_old_supply(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    new = await flow.demand(revision=2)
    await flow.advance("FEED", new)
    flow.current_task = "T2"
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", original)
    assert len(await flow.all_rows("feed")) == 1
    assert len(await flow.all_rows("departure")) == 1


async def test_old_cycle_does_not_use_new_tasks_arrival_for_departure(roller_lifecycle):
    flow = roller_lifecycle
    original, cycle = await flow.ready_supply(count=1)
    old_segment = (await flow.all_rows("feed_segment"))[0]
    flow.current_task = "T2"
    new = await flow.demand(task="T2")
    await flow.advance("FEED", new)
    new_entry = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(new_entry)
    await flow.advance("RACK_CYCLE", new_entry["cycle_id"])
    await flow.feed_success(old_segment)
    await flow.advance("FEED", original)
    assert not [d for d in await flow.all_rows("departure") if d["cycle_id"] == cycle]


async def test_departure_wait_keeps_due_point_then_registers_one_recheck(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply(count=1)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    original = (await flow.all_rows("departure"))[0]
    await flow.decision("departure", original["id"], dict(result="WAIT", retry_after_ms=60000))
    await flow.advance("FEED", demand)
    due = (await flow.all_rows("departure"))[0]["due_at"]
    await flow.advance("RACK_CYCLE", cycle)
    assert (await flow.all_rows("departure"))[0]["due_at"] == due
    async with flow.factory.begin() as session:
        table = flow.store.tables["departure"]
        await session.execute(table.update().where(table.c.id == original["id"]).values(due_at=0))
    await flow.advance("RACK_CYCLE", cycle)
    await flow.advance("FEED", demand)
    requests = await flow.all_rows("departure")
    assert len(requests) == 2
    assert requests[1]["source_ref"] == original["id"]
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
    await flow.advance("FEED", demand)
    original = (await flow.all_rows("departure"))[0]
    await flow.decision("departure", original["id"], data)
    await flow.advance("FEED", demand)
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
        await flow.advance("FEED", demand)
    else:
        original, _ = await flow.ready_supply(count=1)
        demand = await flow.demand(face="B", revision=2)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FEED", original)
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
    await flow.advance("FEED", demand)
    actions = await flow.all_rows("rack_action")
    assert [
        (a["id"], a["transport_task_id"], a["client_request_id"], a["operation_id"])
        for a in actions
    ] == [
        (a["id"], a["transport_task_id"], a["client_request_id"], a["operation_id"])
        for a in originals
    ]
    assert await flow.all_rows("feed") == feeds
    assert actions[-1]["status"] == "SUCCEEDED" and not actions[-1]["pending"]
    assert (actions[-1]["consumed_revision"], actions[-1]["position_revision"]) == (2, 3)
    current = [p for p in await flow.all_rows("position_fact") if p["is_current"]]
    assert [
        (p["location_code"], p["rack_face"], p["source_id"], p["source_revision"]) for p in current
    ] == [(location, face, action["id"], 3)]
    assert (await flow.all_rows("rack_cycle"))[0]["pending"]
    assert progress == "WAITING_FOR_ORIGINAL_POSITION"
