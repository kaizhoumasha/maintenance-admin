import pytest


@pytest.mark.parametrize("raw", ["NOREAD", "A000000001-B"])
async def test_never_admitted_scan_evidence_does_not_block_completed_work(
    manual_bins, manual_completion_tools, raw
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    assert (await flow.scan(raw)).status_code == 200
    held = dict((await flow.rows("manual_bin"))[0])
    assert held["state"] == "HOLD" and held["reason"] == "INVALID_SCAN"
    assert held["task_id"] == "T1" and held["raw_bin_code"] == raw
    assert not held["pending"] and held["admission_operation_id"] is None
    assert held["command_code"] is None
    assert await flow.rows("admission") == []
    assert await flow.rows("scan2_release", component=True) == []

    assert (await flow.scan(timestamp=20)).status_code == 200
    work = next(r for r in await flow.rows("manual_bin") if r["arrival_id"] != held["arrival_id"])
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1", requirements={}))
    await flow.advance("bin", work["arrival_id"])
    assert (await flow.deliver(flow.completion())).status_code == 202
    completion = (await flow.rows("completion"))[0]
    await flow.advance("completion", completion["operation_id"])
    released = next(
        r for r in await flow.rows("manual_bin") if r["arrival_id"] == work["arrival_id"]
    )
    assert released["state"] == "RELEASED" and released["command_code"]

    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    original = (await flow.rows("confirmation"))[0]
    assert original["basis"]["arrivals"] == [
        [work["arrival_id"], released["command_code"], completion["operation_id"]]
    ]
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    assert (
        next(r for r in await flow.rows("manual_bin") if r["arrival_id"] == held["arrival_id"])
        == held
    )
    assert [r["arrival_id"] for r in await flow.rows("admission")] == [work["arrival_id"]]
    releases = await flow.rows("scan2_release", component=True)
    assert [(r["arrival_id"], r["command_code"]) for r in releases] == [
        (work["arrival_id"], released["command_code"])
    ]


@pytest.mark.parametrize("work", [False, True])
async def test_manual_completion_does_not_wait_for_scan2_result(
    manual_bins, manual_completion_tools, work
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision(
        dict(result="WORK_REQUIRED", task_id="T1", requirements={})
        if work
        else dict(result="NO_WORK")
    )
    await flow.advance("bin", arrival)
    if work:
        raw = flow.completion()
        assert (await flow.deliver(raw)).status_code == 202
        await flow.advance("completion", (await flow.rows("completion"))[0]["operation_id"])
    assert (await flow.rows("manual_bin"))[0]["command_code"]
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    assert (await flow.rows("scan2_release", component=True))[0]["pending"]


async def test_target_only_outbound_requires_reliable_transfer_arrival(
    manual_racks, manual_completion_tools
):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=(), target=dict(rack_id="TRANSFER", rack_face="B"))
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.reconcile()
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_outcome(status="FAILED", unknown=True)
    await flow.rack_advance()
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_outcome(revision=2, face="B")
    await flow.rack_advance()
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    assert await flow.rack_advance() == "WAITING_FOR_DEPARTURE"


async def test_direct_report_and_production_motion_are_task_obligations(
    manual_racks, manual_completion_tools
):
    flow = manual_racks
    await flow.ready()
    await flow.plan(faces=("A", "B"))
    await flow.reconcile()
    await flow.rack_outcome()
    await flow.rack_advance()
    await flow.deliver(flow.notification(face="A"))
    await flow.rack_advance()
    await flow.deliver(flow.notification(face="B"))
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_response("rack_report", {}, code="RECORDED")
    assert await flow.consume_report() == "CONSUMED"
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_outcome(status="FAILED", unknown=True)
    await flow.rack_advance()
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.rack_outcome(status="FAILED", revision=2)
    await flow.rack_advance()
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    assert await flow.rows("rack_departure")


async def test_failed_entry_without_arrival_does_not_require_success_for_completed_members(
    manual_racks, manual_completion_tools
):
    flow = manual_racks
    await flow.ready()
    await flow.plan()
    await flow.reconcile()
    await flow.deliver(flow.notification())
    await flow.rack_outcome(status="FAILED")
    await flow.rack_advance()
    assert await flow.rows("rack_report") == []
    await flow.confirm()
    assert len(await flow.confirmations()) == 1


async def test_original_release_result_cannot_replace_confirmation_or_restart_business_wait(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", work["arrival_id"])
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100))
    await flow.confirm(now_ms=1000)
    await flow.result((await flow.rows("manual_bin"))[0]["command_code"])
    await flow.confirm(now_ms=1001)
    assert len(await flow.confirmations()) == 1
    assert (await flow.rows("task"))[0]["status"] == "EXECUTING"
    await flow.confirm(now_ms=1100)
    assert len(await flow.confirmations()) == 2


@pytest.mark.parametrize("state", ["unknown", "wait", "work", "review", "unapplied"])
async def test_unsettled_admission_or_completion_prevents_confirmation(
    manual_bins, manual_completion_tools, state
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    if state == "wait":
        await flow.decision(dict(result="WAIT", retry_after_ms=100))
        await flow.advance("bin", work["arrival_id"])
    if state in {"work", "unapplied"}:
        await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
        await flow.advance("bin", work["arrival_id"])
    if state in {"review", "unapplied"}:
        await flow.deliver(flow.completion())
        if state == "review":
            await flow.advance("completion", (await flow.rows("completion"))[0]["operation_id"])
    await flow.confirm()
    assert await flow.confirmations() == []


async def test_completion_registration_and_application_roll_back_atomically(
    manual_completion_tools,
):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm(rollback=True)
    assert await flow.confirmations() == []
    assert (await flow.rows("task"))[0]["confirmation_operation_id"] is None
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    await flow.confirm(rollback=True)
    assert (await flow.rows("task"))[0]["status"] == "EXECUTING"
    assert (await flow.rows("confirmation"))[0]["state"] == "REQUESTED"
    assert await flow.confirm() == "COMPLETED"


async def test_cancelled_direct_member_needs_original_withdrawal_evidence(
    manual_racks, manual_completion_tools
):
    from sqlalchemy import update

    flow = manual_racks
    await flow.ready()
    await flow.plan()
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["member"]).values(status="CANCELLED"))
    await flow.confirm()
    assert await flow.confirmations() == []


@pytest.mark.parametrize("branch", ["COMPLETED", "PLAN_REVISION_STALE", "BUSINESS_IN_PROGRESS"])
async def test_confirmation_accepts_shared_envelope_timestamp_zero(manual_completion_tools, branch):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    data = dict(result=branch)
    if branch == "PLAN_REVISION_STALE":
        data["current_plan_revision"] = 2
    elif branch == "BUSINESS_IN_PROGRESS":
        data["retry_after_ms"] = 100
    await flow.confirmation_response(data, timestamp=0)
    state = await flow.confirm()
    original = (await flow.rows("confirmation"))[0]
    assert original["decision_code"] == branch
    assert original["response"]["timestamp"] == 0
    if branch == "COMPLETED":
        assert state == "COMPLETED"
    elif branch == "PLAN_REVISION_STALE":
        await flow.bin_plan(2, cancel=True)
        await flow.confirm(now_ms=1001)
        assert len(await flow.confirmations()) == 2
    else:
        await flow.confirm(now_ms=1099)
        assert len(await flow.confirmations()) == 1
        await flow.confirm(now_ms=1100)
        assert len(await flow.confirmations()) == 2
