import asyncio

import pytest
from sqlalchemy import select


async def test_new_arrival_waits_for_original_confirmation_resolution(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    assert (await flow.scan()).status_code == 200
    row = (await flow.rows("manual_bin"))[0]
    assert row["task_id"] == "T1" and row["pending"] and row["state"] == "DEFERRED"
    assert row["admission_operation_id"] is None
    assert row["reason"] == "CONFIRMATION_PENDING"
    await flow.confirm(now_ms=99999)
    assert len(await flow.confirmations()) == 1


@pytest.mark.parametrize("branch", ["PLAN_REVISION_STALE", "BUSINESS_IN_PROGRESS"])
async def test_stale_or_in_progress_resumes_saved_arrival(
    manual_bins, manual_completion_tools, branch
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.scan(timestamp=17)
    work = (await flow.rows("manual_bin"))[0]
    data = dict(result=branch)
    data.update(
        dict(current_plan_revision=2)
        if branch == "PLAN_REVISION_STALE"
        else dict(retry_after_ms=100)
    )
    await flow.confirmation_response(data)
    await flow.confirm()
    if branch == "PLAN_REVISION_STALE":
        await flow.advance("bin", work["arrival_id"])
        assert await flow.rows("admission") == []
        await flow.bin_plan(2, cancel=True)
    await flow.advance("bin", work["arrival_id"])
    work = (await flow.rows("manual_bin"))[0]
    assert work["admission_operation_id"] and work["scanned_at"] == 17
    assert len(await flow.confirmations()) == 1


@pytest.mark.parametrize("new_fact", ["plan", "arrival"])
async def test_old_completed_never_discards_new_plan_or_arrival(
    manual_bins, manual_completion_tools, new_fact
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    if new_fact == "plan":
        await flow.bin_plan(2, cancel=True)
    else:
        await flow.scan()
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "EXECUTING"
    await flow.confirm(now_ms=99999)
    assert len(await flow.confirmations()) == 1
    if new_fact == "arrival":
        work = (await flow.rows("manual_bin"))[0]
        await flow.advance("bin", work["arrival_id"])
        assert await flow.rows("admission") == []
        assert not (await flow.rows("manual_bin"))[0]["command_code"]


@pytest.mark.parametrize(
    "data,http",
    [
        (dict(result="PLAN_REVISION_STALE", current_plan_revision=1), 200),
        (dict(result="PLAN_REVISION_STALE", current_plan_revision=2, retry_after_ms=10), 200),
        (dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=True), 200),
        (dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=0), 200),
        (dict(result="COMPLETED", retry_after_ms=None), 200),
        (dict(result="COMPLETED"), 202),
        (dict(result="COMPLETED"), 503),
    ],
)
async def test_invalid_confirmation_keeps_original_identity(
    manual_bins, manual_completion_tools, data, http
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    original = (await flow.confirmations())[0]
    await flow.scan()
    await flow.confirmation_response(data, http=http)
    await flow.confirm(now_ms=99999)
    work = (await flow.rows("manual_bin"))[0]
    await flow.advance("bin", work["arrival_id"])
    assert await flow.rows("admission") == []
    assert [(r.id, r.operation_id) for r in await flow.confirmations()] == [
        (original.id, original.operation_id)
    ]


async def test_confirmation_branches_keep_original_retry_rules(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100))
    await flow.confirm(now_ms=1000)
    await flow.confirm(now_ms=1099)
    assert len(await flow.confirmations()) == 1
    await flow.confirm(now_ms=1100)
    assert len(await flow.confirmations()) == 2
    await flow.bin_plan(2, cancel=True)
    await flow.confirmation_response(dict(result="PLAN_REVISION_STALE", current_plan_revision=2))
    await flow.confirm(now_ms=1101)
    assert len(await flow.confirmations()) == 3
    await flow.confirmation_response(dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100))
    await flow.confirm(now_ms=1102)
    await flow.bin_plan(3, cancel=True)
    await flow.confirm(now_ms=1103)
    assert len(await flow.confirmations()) == 4


async def test_parallel_confirmations_create_one_original_request(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    barrier = asyncio.Barrier(2)
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(flow.tables["task"]))
            await barrier.wait()

    await asyncio.gather(flow.confirm(before=before), flow.confirm(before=before))
    assert len(await flow.confirmations()) == 1
    assert 1 in attempts


@pytest.mark.parametrize("new_fact", ["plan", "pending_plan", "arrival", "completion"])
async def test_committed_new_fact_wins_over_old_completed_snapshot(
    manual_bins, manual_completion_tools, new_fact
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    saved, release = asyncio.Event(), asyncio.Event()

    async def before_commit():
        saved.set()
        await release.wait()

    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=3 if new_fact == "pending_plan" else 2,
            added_bin_source_racks=[dict(rack_id="R2", rack_face=["A"])],
        ),
    )
    incoming = asyncio.create_task(
        flow.deliver(raw, before_commit=before_commit)
        if new_fact in {"plan", "pending_plan"}
        else flow.deliver(flow.completion(), before_commit=before_commit)
        if new_fact == "completion"
        else flow.scan(before_commit=before_commit)
    )
    await saved.wait()
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(flow.tables["task"]))
            release.set()
            await incoming

    assert await flow.confirm(before=before) == "EXECUTING"
    assert 1 in attempts
    assert (await flow.rows("confirmation"))[0]["state"] == (
        "REQUESTED" if new_fact == "completion" else "CONFLICT"
    )
    assert len(await flow.confirmations()) == 1
    if new_fact == "arrival":
        work = (await flow.rows("manual_bin"))[0]
        assert work["state"] == "DEFERRED" and work["task_id"] == "T1"
        assert not work["command_code"]
    elif new_fact == "plan":
        assert (await flow.rows("task"))[0]["plan_revision"] == 2
    elif new_fact == "completion":
        assert (await flow.rows("completion"))[0]["state"] == "PENDING"
    else:
        assert any(not (row["state"] == "APPLIED") for row in await flow.rows("input"))


async def test_completed_commit_rejects_new_plan_without_reopening_task(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=2,
            added_bin_source_racks=[dict(rack_id="R2", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(raw)).status_code == 409
    assert (await flow.rows("task"))[0]["plan_revision"] == 1
    assert await flow.confirm() == "COMPLETED"
    assert len(await flow.confirmations()) == 1


async def test_duplicate_receipts_do_not_restart_business_wait(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    await flow.confirmation_response(dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100))
    await flow.confirm(now_ms=1000)
    # New message identity repeats exactly the already applied plan, with no new business fact.
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(raw)).status_code == 200
    await flow.confirm(now_ms=1001)
    assert len(await flow.confirmations()) == 1


async def test_rolled_back_arrival_does_not_invalidate_original_confirmation(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    assert (await flow.scan(rollback=True)).status_code == 200
    assert await flow.rows("manual_bin") == []
    assert (await flow.rows("confirmation"))[0]["state"] == "REQUESTED"
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


async def test_completed_task_keeps_original_release_pending_after_successor_starts(
    manual_bins, manual_completion_tools
):
    from app.businesses._components.bin_roller.ports import RollerWorkRef

    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", work["arrival_id"])
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    await flow.ready(task_id="T2")
    original_release = (await flow.rows("scan2_release", component=True))[0]
    await flow.result((await flow.rows("manual_bin"))[0]["command_code"])
    async with flow.factory.begin() as session:
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
        await flow.roller.advance(session, RollerWorkRef("SCAN2_RELEASE", original_release["id"]))
    work = (await flow.rows("manual_bin"))[0]
    assert work["task_id"] == "T1" and work["arrival_id"] == original_release["arrival_id"]
    assert not (await flow.rows("scan2_release", component=True))[0]["pending"]
    tasks = {r["task_id"]: r["status"] for r in await flow.rows("task")}
    assert tasks == {"T1": "COMPLETED", "T2": "PREPARING"}


@pytest.mark.parametrize("second_notice", [False, True])
async def test_duplicate_notice_does_not_hide_next_notice_or_invalidate_original_completed(
    manual_bins, manual_completion_tools, second_notice
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    await flow.advance("bin", work["arrival_id"])
    await flow.deliver(flow.completion())
    await flow.advance("completion", (await flow.rows("completion"))[0]["operation_id"])
    await flow.confirm()
    original = (await flow.confirmations())[0]
    await flow.deliver(flow.completion())
    notice = (await flow.rows("completion"))[-1]
    assert await flow.advance("completion", notice["operation_id"]) == "DUPLICATE"
    await flow.confirmation_response(dict(result="COMPLETED"))
    if not second_notice:
        assert await flow.confirm() == "COMPLETED"
    else:
        saved, release = asyncio.Event(), asyncio.Event()

        async def before_commit():
            saved.set()
            await release.wait()

        raw = flow.completion(
            dict(task_id="T1", bin_code="A000000099", result="NORMAL", completed_at=51)
        )
        incoming = asyncio.create_task(flow.deliver(raw, before_commit=before_commit))
        await saved.wait()
        attempts = []

        async def before(session, attempt):
            attempts.append(attempt)
            if attempt == 0:
                await session.execute(select(flow.tables["task"]))
                release.set()
                await incoming

        assert await flow.confirm(before=before) == "EXECUTING"
        assert 1 in attempts
        assert (await flow.rows("completion"))[-1]["state"] == "PENDING"
    assert [(r.id, r.operation_id) for r in await flow.confirmations()] == [
        (original.id, original.operation_id)
    ]


async def test_early_rack_cutoff_committed_before_old_completion_is_preserved(
    manual_racks, manual_completion_tools
):
    flow = manual_racks
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    saved, release = asyncio.Event(), asyncio.Event()

    async def before_commit():
        saved.set()
        await release.wait()

    incoming = asyncio.create_task(
        flow.deliver(flow.notification(revision=2), before_commit=before_commit)
    )
    await saved.wait()
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(flow.tables["task"]))
            release.set()
            await incoming

    assert await flow.confirm(before=before) == "EXECUTING"
    assert 1 in attempts
    assert not ((await flow.rows("rack_completion"))[0]["state"] == "APPLIED")
    assert len(await flow.confirmations()) == 1


async def test_original_completed_waits_for_duplicate_notice_classification(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    work = (await flow.rows("manual_bin"))[0]
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    await flow.advance("bin", work["arrival_id"])
    await flow.deliver(flow.completion())
    await flow.advance("completion", (await flow.rows("completion"))[0]["operation_id"])
    await flow.confirm()
    await flow.deliver(flow.completion())
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "EXECUTING"
    duplicate = (await flow.rows("completion"))[-1]
    assert await flow.advance("completion", duplicate["operation_id"]) == "DUPLICATE"
    assert await flow.confirm() == "COMPLETED"
    assert len(await flow.confirmations()) == 1


async def test_deterministically_rejected_plan_is_not_pending_business_work(
    manual_completion_tools,
):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="WRONG", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(raw)).status_code == 409
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


@pytest.mark.parametrize("phase", ["first", "business_in_progress", "stale"])
@pytest.mark.parametrize("new_fact", ["arrival", "pending_plan", "completion"])
async def test_committed_fact_blocks_new_confirmation_registration(
    manual_bins, manual_completion_tools, phase, new_fact
):
    from sqlalchemy import text

    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    revision, now_ms = 1, 1000
    if phase != "first":
        await flow.confirm()
        data = (
            dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100)
            if phase == "business_in_progress"
            else dict(result="PLAN_REVISION_STALE", current_plan_revision=2)
        )
        await flow.confirmation_response(data)
        await flow.confirm()
        if phase == "stale":
            await flow.bin_plan(2, cancel=True)
            revision = 2
        now_ms = 1100
    original = [(r.id, r.operation_id) for r in await flow.confirmations()]
    async with flow.factory() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
    saved, release = asyncio.Event(), asyncio.Event()

    async def before_commit():
        saved.set()
        await release.wait()

    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=revision + 2,
            added_bin_source_racks=[dict(rack_id="NEXT", rack_face=["A"])],
        ),
    )
    incoming = asyncio.create_task(
        flow.scan(timestamp=17, before_commit=before_commit)
        if new_fact == "arrival"
        else flow.deliver(
            raw if new_fact == "pending_plan" else flow.completion(), before_commit=before_commit
        )
    )
    await asyncio.wait_for(saved.wait(), timeout=10)
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            assert await session.scalar(text("SHOW transaction_isolation")) == "serializable"
            await session.execute(select(flow.tables["task"]))
            release.set()
            assert (await incoming).status_code == {
                "arrival": 200,
                "pending_plan": 409,
                "completion": 202,
            }[new_fact]

    try:
        await flow.confirm(now_ms=now_ms, before=before)
    finally:
        release.set()
        await incoming
    assert [(r.id, r.operation_id) for r in await flow.confirmations()] == original
    assert 1 in attempts
    if new_fact == "arrival":
        work = (await flow.rows("manual_bin"))[0]
        assert work["state"] == "ADMISSION_PENDING" and work["admission_operation_id"]
        assert work["scanned_at"] == 17
    elif new_fact == "pending_plan":
        assert (await flow.rows("task"))[0]["plan_revision"] == revision
        assert any(r["reason"] == "MISSING_REVISION" for r in await flow.rows("input"))
    else:
        assert (await flow.rows("completion"))[0]["state"] == "PENDING"


@pytest.mark.parametrize("phase", ["first", "business_in_progress", "stale"])
async def test_committed_rack_notice_blocks_new_confirmation_registration(
    manual_racks, manual_completion_tools, phase
):
    flow = manual_racks
    await flow.ready()
    await flow.bin_plan(cancel=True)
    revision, now_ms = 1, 1000
    if phase != "first":
        await flow.confirm()
        data = (
            dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=100)
            if phase == "business_in_progress"
            else dict(result="PLAN_REVISION_STALE", current_plan_revision=2)
        )
        await flow.confirmation_response(data)
        await flow.confirm()
        if phase == "stale":
            await flow.bin_plan(2, cancel=True)
            revision = 2
        now_ms = 1100
    original = [(r.id, r.operation_id) for r in await flow.confirmations()]
    saved, release = asyncio.Event(), asyncio.Event()

    async def before_commit():
        saved.set()
        await release.wait()

    incoming = asyncio.create_task(
        flow.deliver(flow.notification(revision=revision + 1), before_commit=before_commit)
    )
    await asyncio.wait_for(saved.wait(), timeout=10)
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(flow.tables["task"]))
            release.set()
            assert (await incoming).status_code == 202

    try:
        await flow.confirm(now_ms=now_ms, before=before)
    finally:
        release.set()
        await incoming
    assert [(r.id, r.operation_id) for r in await flow.confirmations()] == original
    assert 1 in attempts
    notice = (await flow.rows("rack_completion"))[0]
    assert notice["task_id"] == "T1" and notice["plan_revision"] == revision + 1
    assert not (notice["state"] == "APPLIED")
