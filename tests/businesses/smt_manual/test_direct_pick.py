import json

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
    assert (await flow.deliver(flow.notification(face="A", extra="ignored?"))).status_code == 422
    assert (await flow.deliver(flow.notification(extra="ignored"))).status_code == 202
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
