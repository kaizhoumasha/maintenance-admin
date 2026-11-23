import json

import pytest
from sqlalchemy import select, update

from app.models import ReliableMessage, WorkLine
from tests.helpers import set_message_bodies


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


async def test_idle_transfer_departs_before_parent_and_report(manual_racks):
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
    assert await flow.rack_advance(transfer["id"]) == "WAITING_FOR_DEPARTURE"
    assert (await flow.rows("task"))[0]["status"] == "EXECUTING"
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
        await set_message_bodies(
            session,
            message,
            response_raw=json.dumps(
                dict(
                    operation_id=message.operation_id,
                    code="RECEIVED",
                    timestamp=100,
                    data=dict(transport_task_id=departure["transport_task_id"]),
                )
            ).encode(),
        )
    assert await flow.rack_advance() == "COMPLETED"
    assert (await flow.rows("rack"))[0]["state"] == "COMPLETED"
    await flow.rack_outcome(departure, location="STORE-POS")
    assert await flow.rack_advance() == "COMPLETED"


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


async def test_transfer_open_later_member_blocks_departure_then_original_target_stays_closed(
    manual_host,
):
    flow = manual_host
    await flow.ready()
    await flow.plan(faces=(), target=dict(rack_id="TARGET", rack_face="A"))
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    (entry,) = await flow.rows("rack_action")
    await flow.plan(revision=2)
    await flow.rack_outcome(entry, face="A")
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WORKING"
    assert not await flow.rows("rack_departure")
    await flow.deliver(flow.notification(revision=2))
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WAITING_FOR_DEPARTURE"
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORE")),
    )
    await flow.host.advance("MANUAL_RACK", job["id"])
    depart = next(a for a in await flow.rows("rack_action") if a["kind"] == "DEPART")
    await flow.acknowledge(depart)
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "COMPLETED"
    await flow.plan(revision=3)
    for _ in range(2):
        await flow.host.advance("TASK", "T1")
    assert [r["id"] for r in await flow.rows("rack") if r["role"] == "TRANSFER"] == [job["id"]]


async def test_new_bin_permission_requires_original_applied_target(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan(target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    job = next(r for r in await flow.rows("rack") if r["role"] == "TRANSFER")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == job["id"])
    await flow.scan()
    (arrival,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "ADMISSION_PENDING"
    await flow.rack_outcome(entry, face="B")
    await flow.host.advance("MANUAL_RACK", job["id"])
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "WORK_REQUIRED"
    (applied,) = await flow.rows("manual_bin")
    assert (applied["target_job_id"], applied["target_action_id"]) == (job["id"], entry["id"])
    await flow.deliver(flow.notification())
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WORKING"
    assert not await flow.rows("rack_departure")


async def test_later_member_can_resume_original_return_before_departure_root(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    (entry,) = await flow.rows("rack_action")
    await flow.deliver(flow.notification())
    await flow.host.advance("MANUAL_RACK", job["id"])
    async with flow.factory() as session:
        assert (await flow.host.rack_dispatch.progress(session, job_id=job["id"])).release_requested
    await flow.plan(revision=2, faces=("B",))
    await flow.host.advance("TASK", "T1")
    await flow.rack_outcome(entry, face="A")
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WAITING_FOR_ROTATION"
    member = next(m for m in await flow.rows("member") if m["plan_revision"] == 2)
    rotate = next(a for a in await flow.rows("rack_action") if a["kind"] == "ROTATE")
    assert (member["rack_work_id"], member["preparation_action_id"]) == (job["id"], rotate["id"])
    assert not await flow.rows("rack_departure")


async def test_missing_original_target_association_is_not_rebound_by_task_wakeup(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan(target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    job = next(r for r in await flow.rows("rack") if r["role"] == "TRANSFER")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == job["id"])
    await flow.rack_outcome(entry, face="B")
    await flow.host.advance("MANUAL_RACK", job["id"])
    async with flow.factory.begin() as session:
        inputs = flow.tables["input"]
        await session.execute(
            inputs.update()
            .where(inputs.c.is_plan_record)
            .values(target_job_id=None, target_action_id=None)
        )
    await flow.host.advance("TASK", "T1")
    await flow.scan()
    (arrival,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "ADMISSION_PENDING"
    assert not await flow.rows("rack_departure")


@pytest.mark.parametrize(
    "plan_kind", ["missing_projection", "awaiting_first_plan", "targetless", "inbound"]
)
async def test_bin_grant_uses_original_target_declaration(manual_host, plan_kind):
    flow = manual_host
    await flow.ready(direction="inbound" if plan_kind == "inbound" else "outbound")
    if plan_kind == "missing_projection":
        await flow.plan(target=dict(rack_id="TARGET", rack_face="A"))
        await flow.host.advance("TASK", "T1")
        async with flow.factory.begin() as session:
            task = flow.tables["task"]
            await session.execute(
                task.update().where(task.c.task_id == "T1").values(target_rack=None)
            )
    elif plan_kind == "targetless":
        await flow.plan()
    await flow.scan()
    (arrival,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    state = await flow.host.advance("MANUAL_BIN", arrival["arrival_id"])
    assert state == (
        "WORK_REQUIRED" if plan_kind in ("inbound", "targetless") else "ADMISSION_PENDING"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_command",
        "wrong_command",
        "missing_admission",
        "wrong_decision",
        "missing_completion",
        "wrong_completion",
        "conflicting_completion",
        "no_work",
        "completed_work",
    ],
)
async def test_transfer_handoff_needs_original_bin_release(manual_host, fault):
    flow = manual_host
    await flow.ready()
    await flow.plan(target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    work = next(j for j in await flow.rows("rack") if j["role"] == "TRANSFER")
    enter = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == work["id"])
    await flow.rack_outcome(enter, face="B")
    await flow.host.advance("MANUAL_RACK", work["id"])
    await flow.scan()
    (b,) = await flow.rows("manual_bin")
    if fault in ("completed_work", "missing_completion", "wrong_completion"):
        await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
        assert await flow.host.advance("MANUAL_BIN", b["arrival_id"]) == "WORK_REQUIRED"
        await flow.deliver(flow.completion())
        (notice,) = await flow.rows("completion")
        assert await flow.host.advance("MANUAL_COMPLETION", notice["operation_id"]) == "APPLIED"
    else:
        await flow.decision(dict(result="NO_WORK"))
        assert await flow.host.advance("MANUAL_BIN", b["arrival_id"]) == "RELEASED"
    await flow.deliver(flow.notification())
    async with flow.factory.begin() as session:
        bins, r, inputs = (flow.tables[k] for k in ("manual_bin", "request", "input"))
        changes = {
            "missing_command": dict(command_code=None),
            "wrong_command": dict(command_code="foreign"),
            "missing_admission": dict(admission_operation_id=None),
            "missing_completion": dict(completed_operation_id=None),
        }
        if fault in changes:
            await session.execute(
                bins.update().where(bins.c.arrival_id == b["arrival_id"]).values(**changes[fault])
            )
        elif fault == "wrong_decision":
            await session.execute(
                r.update().where(r.c.kind == "BIN_ADMISSION").values(decision_code="WORK_REQUIRED")
            )
        elif fault == "conflicting_completion":
            await session.execute(
                inputs.insert().values(
                    operation="manual_bin.work_completed@v1",
                    operation_id="other",
                    receipt_id=100,
                    work_line_id=b["work_line_id"],
                    task_id="T1",
                    timestamp=100,
                    payload={},
                    state="APPLIED",
                    bin_code=b["bin_code"],
                    arrival_id=b["arrival_id"],
                    completion_result="NORMAL",
                    completed_at=50,
                )
            )
        elif fault == "wrong_completion":
            await session.execute(
                inputs.update()
                .where(inputs.c.operation_id == notice["operation_id"])
                .values(arrival_id="other")
            )
    await flow.host.advance("MANUAL_RACK", work["id"])
    assert bool(await flow.rows("rack_departure")) is (fault in ("no_work", "completed_work"))


async def test_missing_original_target_action_is_not_rebound_by_task_wakeup(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan(target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    job = next(r for r in await flow.rows("rack") if r["role"] == "TRANSFER")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == job["id"])
    await flow.rack_outcome(entry, face="B")
    await flow.host.advance("MANUAL_RACK", job["id"])
    async with flow.factory.begin() as session:
        inputs = flow.tables["input"]
        await session.execute(
            inputs.update().where(inputs.c.is_plan_record).values(target_action_id=None)
        )
    await flow.host.advance("TASK", "T1")
    plan = (await flow.rows("plan"))[0]
    assert plan["target_job_id"] == job["id"]
    assert plan["target_action_id"] is None


async def test_old_return_at_another_point_does_not_block_original_target_permission(manual_host):
    from app.businesses._components.rack_dispatch.models import RackJob

    flow = manual_host
    await flow.ready()
    await flow.plan(target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    job = next(r for r in await flow.rows("rack") if r["role"] == "TRANSFER")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == job["id"])
    await flow.rack_outcome(entry, face="B")
    await flow.host.advance("MANUAL_RACK", job["id"])
    async with flow.factory.begin() as session:
        session.add(
            RackJob(
                id="old-return",
                owner_domain=flow.host.rack_dispatch.owner_domain,
                work_line_id=flow.lines[0],
                source_kind="MANUAL_RETURN",
                source_ref="old-plan",
                rack_id="OLD-RACK",
                original_cycle_id="old-return",
                task_id="OLD-TASK",
                point="RETURN",
                actual_position={"kind": "RACK_POSITION", "location_code": "RETURN"},
            )
        )
    await flow.scan()
    (arrival,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "WORK_REQUIRED"
