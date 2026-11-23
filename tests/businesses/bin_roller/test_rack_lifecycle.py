import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef, SourceFaceRef, SupplyChange
from tests.helpers import read_history_body


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
        timestamp = json.loads(await read_history_body(session, (message).request_record_id))[
            "timestamp"
        ]
        assert (
            await read_history_body(session, (message).request_record_id)
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


@pytest.mark.parametrize("code,http", [("RECEIVED", 202), ("DUPLICATE", 200)])
async def test_original_ctu02_acceptance_unblocks_next_racks_return_batch(
    roller_lifecycle, code, http
):
    flow = roller_lifecycle
    first, cycle = await flow.ready_supply(count=1)
    rotated = await flow.demand(face="B", revision=2)
    following = await flow.demand(rack="R2", revision=3)
    await flow.advance("FACE", following)
    next_entry = (await flow.all_rows("rack_action"))[-1]
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", first)
    rotation = next(a for a in await flow.all_rows("rack_action") if a["kind"] == "CTU02")
    await flow.rack_result(next_entry)
    await flow.advance("FACE", following)
    assert len(await flow.all_rows("feed")) == 1

    await flow.accepted(rotation, response_task=next_entry["transport_task_id"])
    await flow.advance("RACK_CYCLE", cycle)
    await flow.advance("FACE", following)
    assert len(await flow.all_rows("feed")) == 1

    await flow.accepted(rotation, code=code, http=http)
    await flow.advance("RACK_CYCLE", cycle)
    await flow.advance("FACE", following)
    assert {f["id"] for f in await flow.all_rows("feed")} == {first, following}
    await flow.bins(1)
    await flow.decision("feed", following, dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", following)
    assert [batch["id"] for batch in await flow.all_rows("return_batch")] == [following]

    await flow.rack_result(rotation)
    await flow.advance("RACK_CYCLE", cycle)
    await flow.advance("RACK_CYCLE", next_entry["cycle_id"])
    async with flow.factory() as session:
        dispatch = flow.roller.rack_dispatch
        ready = await dispatch.get_point_preparations(session, work_line_id=1, point="RACK-WORK")
        assert [rack.id for rack in ready] == [cycle]
    assert {f["id"] for f in await flow.all_rows("feed")} == {first, following, rotated}


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("task_cancelled", [False, True])
async def test_cancelled_entry_at_the_work_point_leaves_once(
    roller_lifecycle, late, task_cancelled
):
    from app.reliable import ScopeRef

    flow = roller_lifecycle
    face = await flow.demand()
    await flow.advance("FACE", face)
    entry = (await flow.all_rows("rack_action"))[0]
    cycle = entry["cycle_id"]
    if task_cancelled:
        await flow.accepted(entry)
        async with flow.factory.begin() as session:
            await flow.roller.close_scope(
                session,
                ScopeRef(flow.roller.context.domain, "TASK", flow.current_task),
                reason="TASK_CANCELLED",
            )
    await flow.rack_callback(
        dict(
            transport_task_id=entry["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=1,
            status="CANCELLED",
            final_position=dict(
                kind="RACK_POSITION", location_code="STORAGE" if late else "RACK-WORK"
            ),
        )
    )
    await flow.advance("RACK_CYCLE", cycle)
    if late:
        assert not await flow.all_rows("departure")
        await flow.rack_result(entry, revision=2)
        async with flow.factory() as session:
            upper = await flow.roller.get_pending_upper_id(session, kind="RACK_CYCLE")
            assert RollerWorkRef("RACK_CYCLE", cycle) in await flow.roller.list_pending(
                session, kind="RACK_CYCLE", after_id=None, upper_id=upper, limit=10
            )
        await flow.advance("RACK_CYCLE", cycle)

    assert not await flow.all_rows("feed")
    assert [item["request"]["rack_id"] for item in await flow.all_rows("departure")] == ["R1"]
    assert [action["kind"] for action in await flow.all_rows("rack_action")] == ["CTU01"]
    await flow.finish_departure(cycle)
    await flow.advance("RACK_CYCLE", cycle)
    assert [action["kind"] for action in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert not job.pending
        assert job.actions[0].outcome["status"] == "CANCELLED"
        if task_cancelled:
            assert job.state == "CLOSED_BY_SCOPE"
            assert job.closure_basis["reason"] == "TASK_CANCELLED"


@pytest.mark.parametrize("late", [False, True])
async def test_cancelled_rotation_reclaims_the_point_for_departure_only(roller_lifecycle, late):
    flow = roller_lifecycle
    old, cycle = await flow.ready_supply(count=1)
    new_face = await flow.demand(face="B", revision=2)
    other = await flow.demand(rack="R2", revision=3)
    await flow.advance("FACE", other)
    entry = (await flow.all_rows("rack_action"))[-1]
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", old)
    rotation = (await flow.all_rows("rack_action"))[-1]
    await flow.accepted(rotation)
    await flow.advance("RACK_CYCLE", cycle)
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.rack_callback(
        dict(
            transport_task_id=rotation["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=1,
            status="CANCELLED",
            final_position=(
                dict(kind="RACK_POSITION", location_code="STORAGE") if late else rotation["target"]
            ),
        )
    )
    await flow.advance("RACK_CYCLE", cycle)
    if late:
        assert not await flow.all_rows("departure")
        await flow.rack_result(rotation, revision=2)
        await flow.advance("RACK_CYCLE", cycle)
    assert [d["request"]["rack_id"] for d in await flow.all_rows("departure")] == ["R1"]
    assert new_face not in {f["id"] for f in await flow.all_rows("feed")}
    await flow.finish_departure(cycle)


async def test_task_cancel_clears_the_rack_after_its_frozen_box_move_finishes(roller_lifecycle):
    from sqlalchemy import select

    from app.models import TransportTask
    from app.reliable import ScopeRef

    flow = roller_lifecycle
    _, cycle = await flow.ready_supply(count=1)
    segment = (await flow.all_rows("feed_segment"))[0]
    async with flow.factory() as session:
        operation = await session.scalar(
            select(TransportTask.operation_id).where(
                TransportTask.transport_task_id == segment["transport_task_id"]
            )
        )
    await flow.accepted({**segment, "operation_id": operation})
    async with flow.factory.begin() as session:
        await flow.roller.close_scope(
            session,
            ScopeRef(flow.roller.context.domain, "TASK", flow.current_task),
            reason="TASK_CANCELLED",
        )
    await flow.advance("RACK_CYCLE", cycle)
    assert not await flow.all_rows("departure")
    await flow.feed_success(segment)
    await flow.advance("RACK_CYCLE", cycle)
    await flow.finish_departure(cycle)
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]


@pytest.mark.parametrize(
    "original,condition",
    [
        ("CTU02", "OVERLAP"),
        ("DECISION", "UNSENT"),
        ("CTU03", "OVERLAP"),
        ("CTU03", "DISPATCHED"),
        ("CTU03", "EXPIRED"),
    ],
)
async def test_task_cancel_cleans_up_without_replacing_an_unsent_departure(
    roller_lifecycle, original, condition
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models import ReliableMessage
    from app.reliable import ScopeRef

    flow = roller_lifecycle
    rack = "R2" if condition == "OVERLAP" else "R1"
    if condition == "OVERLAP":
        await awaiting_allocation(flow, "R1")
    face, cycle = await flow.ready_supply(count=1, rack=rack)
    if original == "CTU02":
        await flow.demand(rack=rack, face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", face)
    ready = dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE"))
    if original == "CTU03":
        departure = (await flow.all_rows("departure"))[0]
        await flow.decision("departure", departure["id"], ready)
        await flow.advance("RACK_CYCLE", cycle)
    last = (await flow.all_rows("rack_action"))[-1]
    if original != "DECISION":
        assert last["kind"] == original
    scope = ScopeRef(flow.roller.context.domain, "TASK", flow.current_task)
    async with flow.factory.begin() as session:
        if condition in ("DISPATCHED", "EXPIRED"):
            message = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == last["operation_id"])
            )
            if condition == "DISPATCHED":
                message.dispatch_started_at = datetime.now(UTC)
            else:
                message.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.flush()
        await flow.roller.close_scope(session, scope, reason="TASK_CANCELLED")

    progress = await flow.advance("RACK_CYCLE", cycle)
    departures = await flow.all_rows("departure")
    assert len(departures) == 1
    async with flow.factory() as session:
        if original in ("CTU02", "CTU03"):
            status = await session.scalar(
                select(ReliableMessage.status).where(
                    ReliableMessage.operation_id == last["operation_id"]
                )
            )
            assert status == (
                "ABANDONED"
                if original == "CTU02" or condition in ("DISPATCHED", "EXPIRED")
                else "PENDING"
            )
        else:
            request = await session.get(ReliableMessage, departures[0]["message_id"])
            assert request.status == "PENDING"
    if original == "CTU03":
        if condition == "EXPIRED":
            assert progress == "CLEANUP_SUBMISSION_NEEDS_REVIEW"
            return
        if condition == "DISPATCHED":
            assert len([a for a in await flow.all_rows("rack_action") if a["kind"] == "CTU03"]) == 1
            async with flow.factory() as session:
                assert (await flow.roller.rack_dispatch.progress(session, job_id=cycle)).pending
            return
        await flow.accepted(last)
        await flow.advance("RACK_CYCLE", cycle)
    else:
        await flow.finish_departure(cycle)
    assert len([a for a in await flow.all_rows("rack_action") if a["kind"] == "CTU03"]) == 1
    async with flow.factory() as session:
        stopped = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert stopped.closure_basis["responsibility_release"]["kind"] == "DEPARTURE_ACK"


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


@pytest.mark.parametrize("rotate", [False, True])
async def test_cancelled_rack_at_work_point_leaves_ready_bins_for_the_next_rack(
    roller_lifecycle, rotate
):
    flow = roller_lifecycle
    if rotate:
        first, cycle = await flow.ready_supply(count=1)
        await flow.demand(face="B", revision=2)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FACE", first)
    else:
        first = await flow.demand()
        await flow.advance("FACE", first)
        cycle = (await flow.all_rows("rack_action"))[0]["cycle_id"]
    action = (await flow.all_rows("rack_action"))[-1]
    assert action["kind"] == ("CTU02" if rotate else "CTU01")
    await flow.bins(1)
    await flow.rack_callback(
        dict(
            transport_task_id=action["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=1,
            status="CANCELLED",
            final_position=action["target"],
        )
    )
    await flow.advance("RACK_CYCLE", cycle)
    assert not await flow.all_rows("return_batch")
    assert [row["return_state"] for row in await flow.all_rows("bin_return")] == ["READY"]
    assert len(await flow.all_rows("feed")) == int(rotate)
    departure = (await flow.all_rows("departure"))[0]
    assert departure["request"].get("current_face") is None
    await flow.finish_departure(cycle)
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"


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


async def test_latest_arrival_supersedes_the_old_preparation_before_its_worker_runs(
    roller_lifecycle,
):
    flow = roller_lifecycle
    first, entry, feed = await awaiting_allocation(flow, "R1")
    second = await flow.demand(rack="R2")
    await flow.advance("FACE", second)
    replacement = (await flow.all_rows("rack_action"))[-1]
    await flow.rack_result(replacement, timestamp=2)
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
    await flow.advance("RACK_CYCLE", replacement["cycle_id"])
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    async with flow.factory() as session:
        ready = await flow.roller.rack_dispatch.get_point_preparations(
            session, work_line_id=1, point="RACK-WORK"
        )
        assert [rack.id for rack in ready] == [replacement["cycle_id"]]
    assert {f["id"] for f in await flow.all_rows("feed")} == {first, second}
    await flow.decision("feed", second, dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", second)
    await flow.finish_departure(replacement["cycle_id"])
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    async with flow.factory() as session:
        assert not await flow.roller.rack_dispatch.get_point_preparations(
            session, work_line_id=1, point="RACK-WORK"
        )
    await flow.bins(1)
    row = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", row["id"])
    state = (await flow.all_rows("workline_state"))[0]
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", state["line_run_id"])
    assert any(r["rack_id"] == "D1" for r in await flow.all_rows("rack_cycle"))
    assert [r["kind"] for r in await flow.all_rows("rack_action")] == [
        "CTU01",
        "CTU01",
        "CTU03",
        "CTU01",
    ]


async def test_superseded_departure_decision_does_not_block_drain_entry(roller_lifecycle):
    flow = roller_lifecycle
    first, entry, _ = await awaiting_allocation(flow, "R1")
    second = await flow.demand(rack="R2")
    await flow.advance("FACE", second)
    replacement = (await flow.all_rows("rack_action"))[-1]
    await flow.decision("feed", first, dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", first)
    assert [d["cycle_id"] for d in await flow.all_rows("departure")] == [entry["cycle_id"]]

    await flow.rack_result(replacement)
    await flow.advance("RACK_CYCLE", replacement["cycle_id"])
    await flow.decision("feed", second, dict(result="RACK_FACE_DONE"))
    await flow.advance("FACE", second)
    await flow.finish_departure(replacement["cycle_id"])
    await flow.advance("RACK_CYCLE", entry["cycle_id"])

    await flow.bins(1)
    row = (await flow.all_rows("bin_return"))[0]
    await flow.advance("RETURN", row["id"])
    state = (await flow.all_rows("workline_state"))[0]
    await flow.decide_current(
        state["drain_message_id"], dict(result="READY", racks=[dict(rack_id="D1", rack_face=["A"])])
    )
    await flow.advance("DRAIN", state["line_run_id"])
    assert [r["kind"] for r in await flow.all_rows("rack_action")] == [
        "CTU01",
        "CTU01",
        "CTU03",
        "CTU01",
    ]


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


@pytest.mark.parametrize("rotate", [False, True])
async def test_cancelled_rack_outside_the_point_closes_withdrawn_face(roller_lifecycle, rotate):
    flow = roller_lifecycle
    if rotate:
        first, _ = await flow.ready_supply(count=1)
        face = await flow.demand(face="B", revision=2)
        await flow.feed_success((await flow.all_rows("feed_segment"))[0])
        await flow.advance("FACE", first)
    else:
        face = await flow.demand()
        await flow.advance("FACE", face)
    action = (await flow.all_rows("rack_action"))[-1]
    if rotate:
        await flow.accepted(action)
    revision, side = (2, "B") if rotate else (1, "A")
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1, flow.current_task, "withdraw", (), (SourceFaceRef(revision, "R1", side),)
            ),
        )
    await flow.rack_callback(
        dict(
            transport_task_id=action["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=1,
            status="CANCELLED",
            final_position=dict(kind="RACK_POSITION", location_code="STORAGE"),
        )
    )
    await flow.advance("RACK_CYCLE", action["cycle_id"])
    row = next(row for row in await flow.all_rows("face_work") if row["id"] == face)
    assert row["close_reason"] == "WITHDRAWN"
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=action["cycle_id"])
        assert job.state == "NOT_ARRIVED" and not job.pending
    assert not await flow.all_rows("departure")

    if rotate:
        await flow.rack_result(action, revision=2)
        await flow.advance("RACK_CYCLE", action["cycle_id"])
        assert len(await flow.all_rows("departure")) == 1
        assert len(await flow.all_rows("feed")) == 1


@pytest.mark.parametrize("old_first", [True, False])
async def test_other_rack_anomaly_is_ordered_against_valid_arrival(roller_lifecycle, old_first):
    flow = roller_lifecycle
    first = await flow.demand(rack="R1")
    await flow.advance("FACE", first)
    entry = (await flow.all_rows("rack_action"))[-1]
    second = await flow.demand(rack="R2", revision=2)
    await flow.advance("FACE", second)
    replacement = (await flow.all_rows("rack_action"))[-1]
    assert replacement["cycle_id"] != entry["cycle_id"]
    if not old_first:
        await flow.rack_result(replacement)
    await flow.rack_callback(
        dict(
            transport_task_id=entry["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=1,
            status="SUCCEEDED",
            final_position=entry["target"],
            arrival_face="OTHER",
        )
    )
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    if old_first:
        await flow.rack_result(replacement)
    await flow.advance("RACK_CYCLE", replacement["cycle_id"])
    async with flow.factory() as session:
        ready = await flow.roller.rack_dispatch.get_point_preparations(
            session, work_line_id=1, point="RACK-WORK"
        )
        assert [rack.origin.rack_id for rack in ready] == (["R2"] if old_first else [])
    assert [feed["id"] for feed in await flow.all_rows("feed")] == ([second] if old_first else [])


@pytest.mark.parametrize(
    "template,uncertain,storage_rack,wrong_task,released",
    [
        ("CTU02", False, None, False, False),
        ("CTU02", True, None, False, False),
        ("CTU02", True, "R1", False, False),
        ("CTU03", False, None, False, False),
        ("CTU03", True, None, False, False),
        ("CTU03", False, "R1", False, True),
        ("CTU03", True, "R1", False, True),
        ("CTU03", True, "R2", False, False),
        ("CTU03", True, "R1", True, False),
    ],
)
async def test_rack_submit_rejection_releases_only_original_ctu03_already_in_storage(
    roller_lifecycle, template, uncertain, storage_rack, wrong_task, released, monkeypatch
):
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.businesses._components.bin_roller import retention
    from app.businesses._components.rack_dispatch.models import RackJob
    from app.models import ReliableMessage, TransportTask
    from app.reliable.store import cleanup_terminal
    from tests.helpers import set_message_bodies

    flow = roller_lifecycle
    face, cycle = await flow.ready_supply(count=1)
    if template == "CTU02":
        await flow.demand(face="B", revision=2)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FACE", face)
    if template == "CTU03":
        decision = (await flow.all_rows("departure"))[0]
        await flow.decision(
            "departure",
            decision["id"],
            dict(
                result="READY",
                rack_destination=dict(type="ZONE", location_code="STORAGE"),
            ),
        )
        await flow.advance("RACK_CYCLE", cycle)
    action = next(a for a in await flow.all_rows("rack_action") if a["kind"] == template)
    detail = f"货架({storage_rack})当前已在库区，不能执行搬运任务(CTU03)"
    reason = f"{detail}任务下发失败：{detail}" if storage_rack else "INVALID_DATA"
    async with flow.factory.begin() as session:
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.operation_id == action["operation_id"])
        )
        message.status, message.last_http_status, message.last_verdict = (
            "FAILED",
            422,
            "PEER_TERMINAL",
        )
        message.fail_reason = "REJECTED"
        message.delivery_uncertain = uncertain
        message.attempts = 3 if uncertain else 1
        original_tasks = set(await session.scalars(select(TransportTask.transport_task_id)))
        message_id, request_id = message.id, message.request_record_id
        request = await read_history_body(session, request_id)
        response_task = "other-transport" if wrong_task else action["transport_task_id"]
        response = json.dumps(
            dict(
                operation_id=message.operation_id,
                code="REJECTED",
                timestamp=2,
                data=dict(
                    transport_task_id=response_task,
                    reason_code=reason,
                ),
            )
        ).encode()
        await set_message_bodies(
            session,
            message,
            response_raw=response,
        )
        response_id = message.terminal_response_record_id
    for _ in range(3):
        await flow.advance("RACK_CYCLE", cycle)
    async with flow.factory() as session:
        dispatch = flow.roller.rack_dispatch
        job = await dispatch.progress(session, job_id=cycle)
        if released:
            assert not job.pending and job.review_reason is None
            assert job.closure_reason == "RACK_ALREADY_IN_STORAGE"
            assert job.actual_position is None
            assert job.actions[-1].acceptance_basis is None
            assert await dispatch.point_vacant_for_entry(session, point="RACK-WORK", rack_id="R2")
        else:
            assert job.pending
            blocker = "SUBMISSION_UNCERTAIN" if uncertain else "SUBMIT_REJECTION"
            assert job.review_reason == blocker
            assert job.actual_position == dict(kind="RACK_POSITION", location_code="RACK-WORK")
        message = await session.get(ReliableMessage, message_id)
        assert (message.status, message.last_http_status, message.attempts) == (
            "FAILED",
            422,
            3 if uncertain else 1,
        )
        assert (message.request_record_id, message.terminal_response_record_id) == (
            request_id,
            response_id,
        )
        assert await read_history_body(session, request_id) == request
        assert await read_history_body(session, response_id) == response
        tasks = (await session.scalars(select(TransportTask))).all()
        assert {t.transport_task_id for t in tasks} == original_tasks
        departure = next(t for t in tasks if t.transport_task_id == action["transport_task_id"])
        assert departure.submission_acceptance is None and departure.applied_outcome is None
    assert len(await flow.all_rows("rack_action")) == 2

    if released:
        monkeypatch.setattr(retention, "TABLES", flow.roller.tables)
        candidates = (message_id,)
        async with flow.factory.begin() as session:
            protected = await retention.protected_message_ids(session, candidate_ids=candidates)
            assert (
                await cleanup_terminal(
                    session,
                    now=datetime.now(UTC) + timedelta(days=91),
                    retention_days=90,
                    candidate_ids=candidates,
                    protected_ids=protected,
                )
                == 1
            )
        async with flow.factory() as session:
            assert await dispatch.point_vacant_for_entry(session, point="RACK-WORK", rack_id="R2")
            job = await session.get(RackJob, cycle)
            job.closure_basis = {**job.closure_basis, "submission_message_id": message_id + 1}
            await session.flush()
            assert not await dispatch.point_vacant_for_entry(
                session, point="RACK-WORK", rack_id="R2"
            )

        await flow.rack_callback(
            dict(
                transport_task_id=action["transport_task_id"],
                rack_id="R1",
                kind="RACK_MOVE",
                outcome_revision=1,
                status="FAILED",
                failure_code="RCS_EXECUTION_FAILED",
                final_position=dict(kind="RACK_POSITION", location_code="RACK-WORK"),
            )
        )
        async with flow.factory() as session:
            assert not await dispatch.point_vacant_for_entry(
                session, point="RACK-WORK", rack_id="R2"
            )
