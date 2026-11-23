import pytest
from sqlalchemy import update

from app.models import Device, WorkLine
from tests.helpers import set_message_bodies


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
    assert await flow.consume_report() == "CONSUMED"
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
    assert await flow.consume_report() == "CONSUMED"
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
    assert not await flow.rows("rack_action")
    await flow.rack_advance()
    assert all(r["state"] == "WITHDRAWN" for r in await flow.rows("rack"))
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")
    await flow.plan(revision=2)
    await flow.reconcile()
    failed_entry = (await flow.rows("rack_action"))[-1]
    await flow.rack_outcome(failed_entry, status="FAILED", location="ELSEWHERE")
    await flow.rack_advance(failed_entry["job_id"])
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
    await flow.acknowledge((await flow.rows("rack_action"))[-1])
    assert await flow.rack_advance() == "COMPLETED"
    report = (await flow.rows("rack_report"))[0]
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.ready("T2")
    await flow.plan(task_id="T2")
    await flow.reconcile("T2")
    successor = next(r for r in await flow.rows("rack") if r["task_id"] == "T2")
    await flow.rack_outcome(original_entry, revision=2, location="WRONG")
    assert await flow.rack_advance(original_work["id"]) == "COMPLETED"
    assert (
        next(r for r in await flow.rows("rack") if r["id"] == successor["id"])["state"] == "ACTIVE"
    )
    assert (await flow.rows("rack_report"))[0]["operation_id"] == report["operation_id"]
    async with flow.factory() as session:
        upper = await flow.racks.get_pending_upper_id(session)
        assert set(await flow.racks.list_pending(session, upper_id=upper)) == {
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
    async with flow.factory.begin() as session:
        assert (
            await flow.racks.advance_report(session, operation_id=report["operation_id"])
            == "CONSUMED"
        )
    assert not next(r for r in await flow.rows("rack") if r["id"] == original_work["id"])["pending"]
    assert all(
        r["state"] == "REQUESTED"
        for r in await flow.rows("rack_report")
        if r["operation_id"].startswith("other-")
    )


async def test_independent_report_survives_deleted_line(manual_host):
    from sqlalchemy import delete

    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome(face="A")
    await flow.rack_advance()
    (report,) = await flow.rows("rack_report")
    await flow.rack_response("rack_report", {}, code="RECORDED")
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
        await session.execute(delete(Device).where(Device.work_line_id == flow.lines[0]))
        from app.models import WorkLinePosition

        await session.execute(
            delete(WorkLinePosition).where(WorkLinePosition.work_line_id == flow.lines[0])
        )
        await session.execute(delete(WorkLine).where(WorkLine.id == flow.lines[0]))
    assert await flow.host.advance("RACK_REPORT", report["operation_id"]) == "CONSUMED"


async def test_missing_expected_report_blocks_confirmation_without_a_second_send(
    manual_racks, manual_completion_tools
):
    from sqlalchemy import delete, select

    from app.models import ReliableMessage

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.rack_outcome(face="A")
    await flow.rack_advance()
    (report,) = await flow.rows("rack_report")
    async with flow.factory.begin() as session:
        r = flow.tables["request"]
        await session.execute(
            delete(r).where(
                r.c.operation == report["operation"], r.c.operation_id == report["operation_id"]
            )
        )
    await flow.deliver(flow.notification())
    await flow.rack_advance()
    await flow.confirm()
    assert not await flow.confirmations()
    assert not await flow.rows("rack_report")
    async with flow.factory() as session:
        assert list(
            await session.scalars(
                select(ReliableMessage.id).where(ReliableMessage.operation == report["operation"])
            )
        ) == [report["message_id"]]


async def test_missing_original_member_preparation_cannot_free_rack(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    await flow.rack_outcome(face="A")
    await flow.host.advance("MANUAL_RACK", job["id"])
    await flow.deliver(flow.notification())
    async with flow.factory.begin() as session:
        members = flow.tables["member"]
        await session.execute(
            members.update()
            .where(members.c.rack_work_id == job["id"])
            .values(preparation_action_id=None)
        )
    await flow.host.advance("MANUAL_RACK", job["id"])
    assert not await flow.rows("rack_departure")


async def test_missing_member_from_original_plan_keeps_handoff_unknown(manual_host):
    from sqlalchemy import delete

    flow = manual_host
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    await flow.rack_outcome(face="A")
    await flow.deliver(flow.notification(face="A"))
    async with flow.factory.begin() as session:
        members = flow.tables["member"]
        await session.execute(delete(members).where(members.c.rack_face == "B"))
    await flow.host.advance("MANUAL_RACK", job["id"])
    assert not await flow.rows("rack_departure")


async def test_original_report_identity_survives_missing_row_and_result_review(
    manual_host, monkeypatch
):
    from dataclasses import replace

    from sqlalchemy import delete, select

    from app.models import ReliableMessage

    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    await flow.rack_outcome(face="A")
    await flow.host.advance("MANUAL_RACK", job["id"])
    (report,) = await flow.rows("rack_report")
    async with flow.factory.begin() as session:
        r = flow.tables["request"]
        await session.execute(
            delete(r).where(
                r.c.operation == report["operation"], r.c.operation_id == report["operation_id"]
            )
        )
    transport = flow.host.capabilities.transport
    original = transport.get_result

    async def unavailable(*args, **kwargs):
        return replace(await original(*args, **kwargs), availability="MISSING")

    with monkeypatch.context() as patch:
        patch.setattr(transport, "get_result", unavailable)
        await flow.host.advance("MANUAL_RACK", job["id"])
    assert (await flow.rows("rack_action"))[0]["phase"] == "REVIEW"
    await flow.host.advance("MANUAL_RACK", job["id"])
    async with flow.factory() as session:
        assert list(
            await session.scalars(
                select(ReliableMessage.id).where(ReliableMessage.operation == report["operation"])
            )
        ) == [report["message_id"]]
        assert await flow.host.manual_racks.has_report_obligations(session, task_id="T1")
    assert not await flow.rows("rack_report")


async def test_report_consumption_requires_its_original_saved_report_identity(manual_host):
    flow = manual_host
    await flow.ready()
    await flow.plan()
    await flow.host.advance("TASK", "T1")
    (job,) = await flow.rows("rack")
    await flow.rack_outcome(face="A")
    await flow.host.advance("MANUAL_RACK", job["id"])
    (report,) = await flow.rows("rack_report")
    await flow.rack_response("rack_report", {}, code="RECORDED")
    async with flow.factory.begin() as session:
        members = flow.tables["member"]
        await session.execute(
            members.update()
            .where(members.c.id == job["source_ref"])
            .values(arrival_report_operation_id="other")
        )
    assert await flow.host.advance("RACK_REPORT", report["operation_id"]) == "OWNER_CONFLICT"
    assert (await flow.rows("rack_report"))[0]["state"] == "REQUESTED"


async def test_proven_unsubmitted_entry_rejection_does_not_invent_arrival_report(manual_racks):
    import json

    from sqlalchemy import select

    from app.models import ReliableMessage

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    (entry,) = await flow.rows("rack_action")
    async with flow.factory.begin() as session:
        message = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.business_key == entry["transport_task_id"]
            )
        )
        message.status = "FAILED"
        message.last_http_status = 409
        message.last_verdict = "PEER_TERMINAL"
        message.fail_reason = "CONFLICT"
        await set_message_bodies(
            session,
            message,
            response_raw=json.dumps(
                dict(
                    operation_id=message.operation_id,
                    code="CONFLICT",
                    timestamp=100,
                    data=dict(
                        reason_code="REFERENCE_CONFLICT",
                        transport_task_id=entry["transport_task_id"],
                    ),
                )
            ).encode(),
        )
    assert await flow.rack_advance(entry["job_id"]) == "NOT_ARRIVED"
    assert not await flow.rows("rack_report")
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")


async def test_withdrawn_local_only_candidate_does_not_invent_arrival_report(manual_racks):
    from app.businesses._components.rack_dispatch.models import RackAction

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    (job,) = await flow.rows("rack")
    async with flow.factory.begin() as session:
        session.add(
            RackAction(
                id="original-local",
                job_id=job["id"],
                owner_domain="smt_manual",
                work_line_id=flow.lines[0],
                ordinal=1,
                kind="ENTER",
                source_ref=job["source_ref"],
                completion_rule="APPLIED_RESULT",
                client_request_id="original-local-client",
                target={"kind": "RACK_POSITION", "location_code": job["location_code"]},
                target_face="A",
                basis={"registration_at_cutover": "LOCAL_ONLY"},
            )
        )
        members = flow.tables["member"]
        await session.execute(
            members.update()
            .where(members.c.rack_work_id == job["id"])
            .values(preparation_action_id="original-local")
        )
    assert (
        await flow.deliver(
            flow.message(
                "cancel",
                dict(
                    task_id="T1",
                    cancel_scope="PLAN_MEMBERS",
                    direct_pick_sources=[dict(plan_revision=1, rack_id="DIRECT", rack_face="A")],
                ),
            )
        )
    ).status_code == 202
    assert await flow.rack_advance(job["id"]) == "WITHDRAWN"
    assert not await flow.rows("rack_report")
    async with flow.factory() as session:
        assert not await flow.racks.has_report_obligations(session, task_id="T1")
