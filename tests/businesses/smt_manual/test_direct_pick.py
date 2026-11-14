import json

import pytest
from sqlalchemy import update


async def test_rack_completion_before_original_task_retries_same_identity(manual_racks):
    flow = manual_racks
    raw = json.loads(flow.message("unused", {}))
    raw.update(
        operation="outbound.return_rack.task_completed@v1",
        data=dict(task_id="T1", rack_id="DIRECT", through_plan_revision=1, completed_at=50),
    )
    body = json.dumps(raw).encode()
    response = await flow.deliver(body)
    assert response.status_code == 503
    assert response.body["code"] == "UNAVAILABLE"
    await flow.issue()
    assert (await flow.deliver(body)).status_code == 202
    assert (await flow.deliver(body)).body["code"] == "DUPLICATE"


async def test_face_and_through_revision_completion_do_not_cover_new_members(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    assert (await flow.deliver(flow.notification(face="A"))).status_code == 202
    members = await flow.rows("member")
    assert {m["rack_face"]: m["status"] for m in members} == {"A": "COMPLETED", "B": "PENDING"}
    assert (await flow.deliver(flow.notification(revision=3))).status_code == 202
    assert ((await flow.rows("rack_completion"))[-1]["state"] == "APPLIED") is False
    await flow.plan(revision=2)
    await flow.reconcile()
    assert any(m["status"] == "PENDING" for m in await flow.rows("member"))
    await flow.plan(revision=3)
    await flow.reconcile()
    assert all(m["status"] == "COMPLETED" for m in await flow.rows("member"))
    await flow.plan(revision=4)
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.reconcile()
    assert (
        next(m for m in await flow.rows("member") if m["plan_revision"] == 4)["status"] == "PENDING"
    )


async def test_rack_completion_identity_includes_original_operation(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    face_notice = json.loads(flow.notification(face="A"))
    rack_notice = json.loads(flow.notification(revision=2))
    rack_notice["operation_id"] = face_notice["operation_id"]
    assert (await flow.deliver(json.dumps(face_notice).encode())).status_code == 202
    assert (await flow.deliver(json.dumps(rack_notice).encode())).status_code == 202
    notices = await flow.rows("rack_completion")
    assert len(notices) == 2
    assert sum((n["state"] == "APPLIED") for n in notices) == 1


async def test_rack_callback_contracts_remain_operation_specific(manual_racks):
    flow = manual_racks
    await flow.issue()
    notice = json.loads(flow.notification(face="A", diagnostic_note="first"))
    first = await flow.deliver(json.dumps(notice).encode())
    assert first.status_code == 202
    notice["data"]["diagnostic_note"] = "retry"
    replay = await flow.deliver(json.dumps(notice).encode())
    assert replay.status_code == 200
    assert replay.body == first.body | {"code": "DUPLICATE"}
    notice["data"]["rack_face"] = "B"
    assert (await flow.deliver(json.dumps(notice).encode())).status_code == 409
    for field, value in (("through_plan_revision", 2), ("slot_ids", ["S1"])):
        response = await flow.deliver(flow.notification(face="A", **{field: value}))
        assert response.status_code == 422
        assert response.body["data"] == {"reason_code": "INVALID_DATA"}
    assert (await flow.deliver(flow.notification(extra="ignored"))).status_code == 202
    for field, value in (("plan_revision", 2), ("rack_face", "A"), ("slot_ids", ["S1"])):
        notice = json.loads(flow.notification())
        notice["data"][field] = value
        response = await flow.deliver(json.dumps(notice).encode())
        assert response.status_code == 422
        assert response.body["data"] == {"reason_code": "INVALID_DATA"}
    for face in (None, "A"):
        for completed_at in (0, 101):
            raw = json.loads(flow.notification(face=face))
            raw["data"]["completed_at"] = completed_at
            assert (await flow.deliver(json.dumps(raw).encode())).status_code == 422
    raw = json.loads(flow.notification(face="A"))
    raw["timestamp"] = 0
    assert (await flow.deliver(json.dumps(raw).encode())).status_code == 422
    await flow.issue("INBOUND", direction="inbound", line=2)
    for face in (None, "A"):
        assert (
            await flow.deliver(flow.notification(task_id="INBOUND", face=face))
        ).status_code == 422


async def test_mixed_face_members_reuse_only_their_own_preparation(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    (entry,) = await flow.rows("rack_action")
    await flow.rack_outcome(face="A")
    await flow.host.advance("MANUAL_RACK", job["id"])
    await flow.plan(revision=2, faces=("B", "A"))
    await flow.host.advance("TASK", "T1")
    later = {m["rack_face"]: m for m in await flow.rows("member") if m["plan_revision"] == 2}
    assert later["A"]["preparation_action_id"] == entry["id"]
    assert later["B"]["preparation_action_id"] is None
    await flow.deliver(flow.notification(face="A", revision=1))
    assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WORKING"
    await flow.deliver(flow.notification(face="A", revision=2))
    for _ in range(2):
        assert await flow.host.advance("MANUAL_RACK", job["id"]) == "WAITING_FOR_ROTATION"
    enter, rotate = await flow.rows("rack_action")
    assert (enter["id"], rotate["kind"], rotate["target_face"]) == (entry["id"], "ROTATE", "B")
    later = next(m for m in await flow.rows("member") if m["id"] == later["B"]["id"])
    assert later["preparation_action_id"] == rotate["id"]


@pytest.mark.parametrize("binding", ["missing", "incompatible"])
async def test_orphaned_same_rack_member_retains_return(manual_host, binding):
    flow = manual_host
    await flow.ready()
    await flow.plan(faces=("A", "B"), target=dict(rack_id="TARGET", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    jobs = await flow.rows("rack")
    job = next(j for j in jobs if j["role"] == "RETURN")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == job["id"])
    await flow.rack_outcome(entry, face="A")
    await flow.deliver(flow.notification(face="A"))
    async with flow.factory.begin() as session:
        t = flow.tables["member"]
        await session.execute(
            t.update()
            .where(t.c.rack_face == "B")
            .values(
                rack_work_id=(
                    "missing-job"
                    if binding == "missing"
                    else next(j["id"] for j in jobs if j["role"] == "TRANSFER")
                )
            )
        )
    await flow.host.advance("MANUAL_RACK", job["id"])
    assert not await flow.rows("rack_departure")


async def test_prior_unfulfilled_member_stays_with_original_not_arrived_job(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    (prior,) = await flow.rows("rack")
    await flow.rack_outcome(status="CANCELLED", location="STORE")
    assert await flow.rack_advance(prior["id"]) == "NOT_ARRIVED"
    await flow.plan(revision=2)
    await flow.reconcile()
    later = next(j for j in await flow.rows("rack") if j["id"] != prior["id"])
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == later["id"])
    await flow.rack_outcome(entry, face="A")
    await flow.deliver(flow.notification(face="A", revision=2))
    assert await flow.rack_advance(later["id"]) == "WAITING_FOR_DEPARTURE"
    member = next(m for m in await flow.rows("member") if m["plan_revision"] == 1)
    assert (member["rack_work_id"], member["status"]) == (prior["id"], "PENDING")


async def test_missing_original_trigger_cannot_capture_later_grant(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    member = flow.tables["member"]
    async with flow.factory.begin() as db:
        await db.execute(member.update().values(preparation_action_id=None))
    await flow.plan(revision=2)
    await flow.host.advance("TASK", "T1")
    later = next(m for m in await flow.rows("member") if m["plan_revision"] == 2)
    assert later["rack_work_id"] is None
