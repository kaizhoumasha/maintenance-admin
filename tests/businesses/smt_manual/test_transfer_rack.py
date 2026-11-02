import json

from sqlalchemy import select, update

from app.models import ReliableMessage, WorkLine


async def test_optional_transfer_rack_does_not_gate_unrelated_work(manual_racks):
    flow = manual_racks
    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, flow.lines[0])
        line.position_bindings = {
            k: v for k, v in line.position_bindings.items() if k != "TRANSFER_RACK"
        }
    await flow.ready()
    assert (await flow.plan()).status_code == 202
    await flow.reconcile()
    racks = await flow.rows("rack")
    assert [(r["role"], r["rack_id"]) for r in racks] == [("RETURN", "DIRECT")]


async def test_first_plan_transfer_creates_its_original_entry(manual_racks):
    flow = manual_racks
    await flow.ready()
    assert (await flow.plan(target=dict(rack_id="TRANSFER", rack_face="B"))).status_code == 202
    await flow.reconcile()
    assert {r["role"] for r in await flow.rows("rack")} == {"RETURN", "TRANSFER"}
    assert len(await flow.rows("rack_action")) == 2


async def test_transfer_departure_requires_task_completion_and_original_entry(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(target=dict(rack_id="TRANSFER", rack_face="B"))
    await flow.reconcile()
    transfer = next(r for r in await flow.rows("rack") if r["role"] == "TRANSFER")
    action = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == transfer["id"])
    await flow.deliver(flow.notification())
    await flow.rack_advance(transfer["id"])
    assert await flow.rows("rack_departure") == []
    await flow.rack_outcome(action, face="B")
    assert await flow.rack_advance(transfer["id"]) == "WORKING"
    assert await flow.rows("rack_departure") == []
    # M4 owns this authoritative task completion, independently of source-rack motion.
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    assert await flow.rack_advance(transfer["id"]) == "WAITING_FOR_DEPARTURE"
    departures = await flow.rows("rack_departure")
    assert len(departures) == 1 and departures[0]["request_snapshot"]["rack_id"] == "TRANSFER"


async def test_departure_wait_and_unknown_keep_original_context(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.deliver(flow.notification())
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"
    first = (await flow.rows("rack_departure"))[0]
    assert set(first["request_snapshot"]) == {
        "workline_code",
        "rack_id",
        "current_location",
        "current_face",
    }
    assert await flow.rack_advance(now_ms=99999) == "WAITING_FOR_DEPARTURE"
    assert len(await flow.rows("rack_departure")) == 1
    await flow.rack_response("rack_departure", dict(result="WAIT", retry_after_ms=100))
    await flow.rack_advance(now_ms=1000)
    await flow.rack_advance(now_ms=1099)
    assert len(await flow.rows("rack_departure")) == 1
    assert (await flow.rows("rack_departure"))[0]["due_at_ms"] == 1100
    await flow.rack_advance(now_ms=1100)
    second = (await flow.rows("rack_departure"))[-1]
    assert second["operation_id"] != first["operation_id"]
    assert second["request_snapshot"] == first["request_snapshot"]
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    assert await flow.rack_advance(now_ms=1101) == "WAITING_FOR_DEPARTURE_RESULT"
    departure = (await flow.rows("rack_action"))[-1]
    assert departure["kind"] == "DEPART" and departure["target"]["location_code"] == "STORAGE"
    async with flow.factory.begin() as session:
        message = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.business_key == departure["transport_task_id"]
            )
        )
        message.status, message.last_http_status, message.last_verdict = "DONE", 202, "DELIVERED"
        message.response_raw = json.dumps(
            dict(
                operation_id=message.operation_id,
                code="RECEIVED",
                timestamp=100,
                data=dict(transport_task_id=departure["transport_task_id"]),
            )
        ).encode()
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE_RESULT"
    assert (await flow.rows("rack"))[0]["state"] == "ACTIVE"
    assert len(await flow.rows("rack_action")) == 2
    await flow.rack_outcome(departure, location="STORE-POS")
    assert await flow.rack_advance() == "DEPARTED"


async def test_new_task_completion_can_recheck_saved_wait_before_its_deadline(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.deliver(flow.notification())
    await flow.rack_advance()
    await flow.rack_response("rack_departure", dict(result="WAIT", retry_after_ms=100))
    await flow.rack_advance(now_ms=1000)
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.rack_advance(now_ms=1001)
    requests = await flow.rows("rack_departure")
    assert len(requests) == 2
    assert requests[0]["due_at_ms"] == 1100
    assert requests[0]["request_snapshot"] == requests[1]["request_snapshot"]
    assert requests[0]["operation_id"] != requests[1]["operation_id"]


async def test_return_rack_never_receives_roller_bins(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance()
    assert await flow.rows("return_batch", component=True) == []
    assert await flow.rows("rack_cycle", component=True) == []
    assert {a["kind"] for a in await flow.rows("rack_action")} == {"ENTER"}


async def test_failed_rotation_known_position_departure_uses_null_face(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance()
    await flow.deliver(flow.notification(face="A"))
    await flow.rack_advance()
    await flow.rack_outcome(status="FAILED")
    await flow.deliver(flow.notification(face="B"))
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"
    request = (await flow.rows("rack_departure"))[0]["request_snapshot"]
    assert request["current_face"] is None
    assert request["current_location"] == dict(type="RACK_POSITION", location_code="RETURN")


async def test_unknown_motion_retains_original_identity_and_requires_new_applied_result(
    manual_racks,
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    original = (await flow.rows("rack_action"))[0]
    await flow.rack_outcome(status="FAILED", unknown=True)
    await flow.deliver(flow.notification())
    assert await flow.rack_advance() == "POSITION_UNKNOWN"
    assert await flow.rack_advance(now_ms=999999) == "POSITION_UNKNOWN"
    assert await flow.rows("rack_departure") == []
    assert await flow.rows("rack_report") == []
    assert (await flow.rows("rack_action"))[0]["client_request_id"] == original["client_request_id"]
    await flow.rack_outcome(revision=2)
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"
    assert len(await flow.rows("rack_action")) == 1


async def test_departure_retries_sort_after_original_uuid_in_same_millisecond(
    manual_racks, monkeypatch
):
    from datetime import UTC, datetime

    from app.businesses.smt_manual import manual_rack_flow
    from app.core.identifiers import uuid7

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    assert await flow.rack_advance() == "WORKING"
    await flow.deliver(flow.notification())
    clock = datetime(2026, 11, 2, tzinfo=UTC)
    high = uuid7(clock, randbits=lambda _: (1 << 74) - 1)
    low = uuid7(clock, randbits=lambda _: 0)
    candidates = iter((high, low))
    monkeypatch.setattr(
        manual_rack_flow,
        "uuid7",
        lambda now=None: next(candidates) if now is None else uuid7(now, randbits=lambda _: 0),
    )
    assert await flow.rack_advance(now_ms=1000) == "WAITING_FOR_DEPARTURE"
    await flow.rack_response("rack_departure", dict(result="WAIT", retry_after_ms=1))
    assert await flow.rack_advance(now_ms=1000) == "WAITING_FOR_DEPARTURE"
    assert await flow.rack_advance(now_ms=1001) == "WAITING_FOR_DEPARTURE"
    attempts = await flow.rows("rack_departure")
    assert len(attempts) == 2 and attempts[0]["operation_id"] == str(high)
    current = (await flow.rows("rack"))[0]["departure_operation_id"]
    assert current == max(r["operation_id"] for r in attempts)
    assert current > str(high)
    assert attempts[0]["due_at_ms"] == 1001
