import pytest
from sqlalchemy import update

from app.models import WorkLine


async def test_direct_pick_requires_original_rack_arrival(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    assert len(await flow.rows("rack_action")) == 1
    assert await flow.rows("rack_report") == []
    assert await flow.rack_advance() == "WAITING_FOR_ENTRY"
    assert await flow.rows("rack_report") == []
    assert (await flow.rack_outcome()).status_code == 202
    assert await flow.rack_advance() == "WORKING"
    report = (await flow.rows("rack_report"))[0]
    assert report["state"] == "REQUESTED"
    await flow.rack_response("rack_report", {}, code="RECORDED")
    await flow.rack_advance()
    assert (await flow.rows("rack_report"))[0]["state"] == "CONSUMED"


async def test_arrival_report_obligation_is_separate_from_rack_motion(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance()
    assert (await flow.deliver(flow.notification(face="A"))).status_code == 202
    assert await flow.rack_advance() == "WAITING_FOR_ROTATION"
    assert [a["kind"] for a in await flow.rows("rack_action")] == ["ENTER", "ROTATE"]
    assert (await flow.rows("rack_report"))[0]["state"] == "REQUESTED"
    await flow.rack_outcome(face="B")
    await flow.rack_advance()
    await flow.deliver(flow.notification(face="B"))
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"
    assert len(await flow.rows("rack_departure")) == 1


@pytest.mark.parametrize("location,face", [("WRONG", "A"), ("RETURN", "B")])
async def test_wrong_target_observation_never_authorizes_production(manual_racks, location, face):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome(location=location, face=face)
    assert await flow.rack_advance() == "POSITION_CONFLICT"
    assert await flow.rows("rack_report") == []
    assert await flow.rows("rack_departure") == []
    action = (await flow.rows("rack_action"))[0]
    assert action["actual_position"]["location_code"] == location
    assert action["actual_face"] == face
    await flow.rack_outcome(revision=2)
    assert await flow.rack_advance() == "WORKING"


@pytest.mark.parametrize("location,face", [("WRONG", "A"), ("RETURN", "B")])
async def test_newer_observation_survives_coalesced_unknown_consumption(
    manual_racks, location, face
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    original = (await flow.rows("rack_action"))[0]
    assert (await flow.rack_outcome(status="FAILED", unknown=True)).status_code == 202
    assert (await flow.rack_outcome(revision=2, location=location, face=face)).status_code == 202
    for _ in range(2):
        state = await flow.rack_advance()
        action = (await flow.rows("rack_action"))[0]
        assert action["id"] == original["id"]
        assert action["actual_position"] == dict(kind="RACK_POSITION", location_code=location)
        assert action["actual_face"] == face
        assert state == "POSITION_CONFLICT"
        assert action["outcome_revision"] == 1 and action["observation_revision"] == 2
        assert await flow.rows("rack_report") == []
        assert await flow.rows("rack_departure") == []
        assert len(await flow.rows("rack_action")) == 1


async def test_registered_entry_keeps_report_obligation_before_result_consumption(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    async with flow.factory() as session:
        assert await flow.racks.has_report_obligations(session, task_id="T1")
    await flow.rack_outcome()
    async with flow.factory() as session:
        assert await flow.racks.has_report_obligations(session, task_id="T1")
    await flow.rack_advance()
    await flow.rack_response("rack_report", {}, code="RECORDED")
    await flow.rack_advance()
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")


async def test_unstarted_cancelled_members_and_failed_entry_do_not_invent_reports(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    cancel = dict(
        task_id="T1",
        cancel_scope="PLAN_MEMBERS",
        direct_pick_sources=[dict(plan_revision=1, rack_id="DIRECT", rack_face="A")],
    )
    assert (await flow.deliver(flow.message("cancel", cancel))).status_code == 202
    await flow.reconcile()
    assert await flow.rows("rack") == []
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")
    await flow.plan(revision=2)
    await flow.reconcile()
    await flow.rack_outcome(status="FAILED", location="ELSEWHERE")
    await flow.rack_advance()
    assert await flow.rows("rack_report") == []
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")


async def test_disabled_line_retains_original_result_reports_and_safe_departure(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    await flow.rack_outcome()
    assert await flow.rack_advance() == "WORKING"
    assert len(await flow.rows("rack_report")) == 1
    await flow.deliver(flow.notification(face="A"))
    assert await flow.rack_advance() == "NEW_WORK_BLOCKED"
    assert len(await flow.rows("rack_action")) == 1
    await flow.deliver(flow.notification(face="B"))
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"


async def test_result_consumption_and_report_intent_rollback_together(manual_racks):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance(rollback=True)
    assert await flow.rows("rack_report") == []
    assert (await flow.rows("rack_action"))[0]["outcome_revision"] is None
    await flow.rack_advance()
    assert len(await flow.rows("rack_report")) == 1
    assert (await flow.rows("rack_action"))[0]["outcome_revision"] == 1


async def test_old_report_and_late_result_keep_original_rack_after_current_task_switch(
    manual_racks,
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    original_work = (await flow.rows("rack"))[0]
    original_entry = (await flow.rows("rack_action"))[0]
    await flow.rack_outcome()
    await flow.rack_advance()
    await flow.deliver(flow.notification())
    await flow.rack_advance()
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="RACK_POSITION", location_code="EXIT")),
    )
    await flow.rack_advance()
    await flow.rack_outcome()
    assert await flow.rack_advance() == "DEPARTED"
    report = (await flow.rows("rack_report"))[0]
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.ready("T2")
    await flow.plan(task_id="T2")
    await flow.reconcile("T2")
    successor = next(r for r in await flow.rows("rack") if r["task_id"] == "T2")
    await flow.rack_outcome(original_entry, revision=2, location="WRONG")
    assert await flow.rack_advance(original_work["id"]) == "DEPARTED"
    assert (
        next(r for r in await flow.rows("rack") if r["id"] == successor["id"])["state"] == "ACTIVE"
    )
    assert (await flow.rows("rack_report"))[0]["operation_id"] == report["operation_id"]
    async with flow.factory() as session:
        upper = await flow.racks.get_pending_upper_id(session)
        assert set(await flow.racks.list_pending(session, upper_id=upper)) == {
            original_work["id"],
            successor["id"],
        }
    await flow.rack_response("rack_report", {}, code="DUPLICATE")
    requests = flow.tables["request"]
    async with flow.factory.begin() as session:
        for key, value in (("task_id", "T2"), ("work_line_id", flow.lines[1])):
            await session.execute(
                requests.insert().values(
                    **(
                        dict(report)
                        | {key: value, "operation_id": "other-" + key, "action_id": key}
                    )
                )
            )
    await flow.rack_advance(original_work["id"])
    assert not next(r for r in await flow.rows("rack") if r["id"] == original_work["id"])["pending"]
    assert all(
        r["state"] == "REQUESTED"
        for r in await flow.rows("rack_report")
        if r["operation_id"].startswith("other-")
    )
