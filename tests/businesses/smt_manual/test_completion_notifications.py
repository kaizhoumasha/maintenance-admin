import json

import pytest


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize("result", ["NORMAL", "NG"])
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_completed_receipt_precedes_application(manual_bins, result, direction):
    flow = manual_bins
    await flow.ready_for_bin_work(direction=direction)
    assert (await flow.scan()).status_code == 200
    work = (await flow.rows("manual_bin"))[0]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", work["arrival_id"])
    raw = flow.completion(dict(task_id="T1", bin_code="A000000001", result=result, completed_at=50))
    response = await flow.deliver(raw)
    assert response.status_code == 202
    assert not await flow.rows("scan2_release", component=True)
    notice = (await flow.rows("completion"))[0]
    assert notice["state"] == "PENDING" and notice["arrival_id"] is None
    assert await flow.advance("completion", notice["operation_id"], rollback=True) == "APPLIED"
    assert not await flow.rows("scan2_release", component=True)
    assert await flow.advance("completion", notice["operation_id"]) == "APPLIED"
    assert await flow.advance("completion", notice["operation_id"]) == "APPLIED"
    assert (await flow.rows("manual_bin"))[0]["completed_result"] == result
    assert len(await flow.rows("scan2_release", component=True)) == 1
    assert (await flow.deliver(raw)).body["code"] == "DUPLICATE"


async def test_early_completion_is_never_autobound(manual_bins):
    flow = manual_bins
    await flow.ready_for_bin_work()
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 202
    operation_id = json.loads(raw)["operation_id"]
    assert await flow.advance("completion", operation_id) == "REVIEW"
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", arrival)
    assert await flow.advance("completion", operation_id) == "REVIEW"
    assert not await flow.rows("scan2_release", component=True)


@pytest.mark.parametrize(
    "change",
    [
        {"result": None},
        {"completed_at": 0},
        {"completed_at": True},
        {"completed_at": 101},
        {"bin_code": None},
        {"task_id": 1},
    ],
)
async def test_manual_callback_error_and_field_policy(manual_bins, change):
    flow = manual_bins
    await flow.ready()
    data = dict(task_id="T1", bin_code="A000000001", result="NORMAL", completed_at=50)
    data.update(change)
    response = await flow.deliver(flow.completion(data))
    assert response.status_code == 422 and response.body["data"]["reason_code"] == "INVALID_DATA"
    assert not await flow.rows("completion")


async def test_completion_duplicates_and_conflicts_keep_first_work(manual_bins):
    flow = manual_bins
    await flow.ready_for_bin_work()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", arrival)
    first = flow.completion()
    assert (await flow.deliver(first)).status_code == 202
    first_id = json.loads(first)["operation_id"]
    assert await flow.advance("completion", first_id) == "APPLIED"
    saved = (await flow.rows("manual_bin"))[0]
    for result, expected in (("NORMAL", "DUPLICATE"), ("NG", "REVIEW")):
        raw = flow.completion(
            dict(task_id="T1", bin_code="A000000001", result=result, completed_at=60)
        )
        assert (await flow.deliver(raw)).status_code == 202
        assert await flow.advance("completion", json.loads(raw)["operation_id"]) == expected
    assert (await flow.rows("manual_bin"))[0] == saved
    assert len(await flow.rows("scan2_release", component=True)) == 1


async def test_new_arrival_does_not_inherit_old_release(manual_bins):
    flow = manual_bins
    await flow.ready_for_bin_work()
    assert (await flow.scan()).status_code == 200
    first = (await flow.rows("manual_bin"))[0]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", first["arrival_id"])
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 202
    assert await flow.advance("completion", json.loads(raw)["operation_id"]) == "APPLIED"
    old = (await flow.rows("manual_bin"))[0]
    # Intervening content proves a different original arrival without requiring a Result gate.
    assert (await flow.scan("NOREAD", timestamp=20)).status_code == 200
    assert (await flow.scan(timestamp=30)).status_code == 200
    current = (await flow.rows("manual_bin"))[-1]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", current["arrival_id"])
    repeated = flow.completion()
    assert (await flow.deliver(repeated)).status_code == 202
    repeated_id = json.loads(repeated)["operation_id"]
    assert await flow.advance("completion", repeated_id) == "DUPLICATE"
    assert (await flow.rows("completion"))[-1]["arrival_id"] == old["arrival_id"]
    assert len(await flow.rows("scan2_release", component=True)) == 1
    await flow.result(old["command_code"])
    assert len(await flow.rows("scan2_release", component=True)) == 1
    fresh = flow.completion(dict(task_id="T1", bin_code="A000000001", result="NG", completed_at=80))
    assert (await flow.deliver(fresh)).status_code == 202
    assert await flow.advance("completion", json.loads(fresh)["operation_id"]) == "APPLIED"
    assert len(await flow.rows("scan2_release", component=True)) == 2


@pytest.mark.parametrize("setup", ["ambiguous", "wrong_task", "no_work"])
async def test_completion_binds_only_unique_original_work_required(manual_bins, setup):
    flow = manual_bins
    await flow.ready_for_bin_work()
    assert (await flow.scan()).status_code == 200
    first = (await flow.rows("manual_bin"))[0]
    await flow.decision(
        {"result": "NO_WORK"}
        if setup == "no_work"
        else {"result": "WORK_REQUIRED", "task_id": "T1"}
    )
    await flow.advance("bin", first["arrival_id"])
    if setup == "ambiguous":
        assert (await flow.scan("NOREAD", timestamp=20)).status_code == 200
        assert (await flow.scan(timestamp=30)).status_code == 200
        current = (await flow.rows("manual_bin"))[-1]
        await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
        await flow.advance("bin", current["arrival_id"])
    if setup == "wrong_task":
        assert (await flow.issue("T2")).status_code == 202
    raw = flow.completion(
        dict(
            task_id="T2" if setup == "wrong_task" else "T1",
            bin_code="A000000001",
            result="NORMAL",
            completed_at=50,
        )
    )
    assert (await flow.deliver(raw)).status_code == 202
    assert await flow.advance("completion", json.loads(raw)["operation_id"]) == "REVIEW"
    assert len(await flow.rows("scan2_release", component=True)) == int(setup == "no_work")


async def test_replayed_early_evidence_retains_its_first_review(manual_bins):
    flow = manual_bins
    await flow.ready_for_bin_work()
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 202
    original_id = json.loads(raw)["operation_id"]
    assert await flow.advance("completion", original_id) == "REVIEW"
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", arrival)
    repeated = flow.completion()
    assert (await flow.deliver(repeated)).status_code == 202
    assert await flow.advance("completion", json.loads(repeated)["operation_id"]) == "REVIEW"
    assert not await flow.rows("scan2_release", component=True)


async def test_callback_effective_content_and_identity_drift(manual_bins):
    flow = manual_bins
    await flow.ready()
    raw = flow.completion()
    response = await flow.deliver(raw)
    assert response.status_code == 202
    body = json.loads(raw)
    body["data"]["note"] = "unknown annotation"
    duplicate = await flow.deliver(json.dumps(body).encode())
    assert duplicate.body["code"] == "DUPLICATE"
    assert duplicate.body["timestamp"] == response.body["timestamp"]
    body["data"]["result"] = "NG"
    conflict = await flow.deliver(json.dumps(body).encode())
    assert conflict.status_code == 409
    assert conflict.body["data"]["reason_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(await flow.rows("completion")) == 1


async def test_completion_receipt_outer_rollback_keeps_no_pending_fact(manual_bins):
    from sqlalchemy import select

    from app.models import CallbackReceipt

    flow = manual_bins
    await flow.ready()
    raw = flow.completion()
    assert (await flow.deliver(raw, rollback=True)).status_code == 202
    assert not await flow.rows("completion")
    async with flow.factory() as session:
        assert (
            await session.scalar(
                select(CallbackReceipt.id).where(
                    CallbackReceipt.operation_id == json.loads(raw)["operation_id"]
                )
            )
            is None
        )


async def test_concurrent_completion_application_preserves_one_release(
    manual_bins, overlapping_manual_snapshots
):
    import asyncio

    flow = manual_bins
    await flow.ready_for_bin_work()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", arrival)
    bodies = [flow.completion() for _ in range(2)]
    assert all(
        response.status_code == 202
        for response in await asyncio.gather(*(flow.deliver(body) for body in bodies))
    )
    outcomes = await asyncio.gather(
        *(
            flow.advance(
                "completion",
                json.loads(body)["operation_id"],
                before=overlapping_manual_snapshots.before,
            )
            for body in bodies
        )
    )
    assert sorted(outcomes) == ["APPLIED", "DUPLICATE"]
    assert len(await flow.rows("scan2_release", component=True)) == 1
    assert 1 in overlapping_manual_snapshots.attempts


async def test_first_application_uses_original_work_even_if_receipt_was_earlier(manual_bins):
    flow = manual_bins
    await flow.ready_for_bin_work()
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 202
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WORK_REQUIRED", "task_id": "T1"})
    await flow.advance("bin", arrival)
    assert await flow.advance("completion", json.loads(raw)["operation_id"]) == "APPLIED"


@pytest.mark.parametrize("timestamp", [None, True, -1, 2**63])
async def test_manual_callback_uses_operation_specific_envelope_errors(manual_bins, timestamp):
    flow = manual_bins
    await flow.ready()
    response = await flow.deliver(flow.completion(timestamp=timestamp))
    assert response.status_code == 422
    assert response.body["data"]["reason_code"] == "INVALID_ENVELOPE"
    assert not await flow.rows("completion")


async def test_unresolvable_original_task_is_unavailable_without_a_receipt(manual_bins):
    flow = manual_bins
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 503
    assert not await flow.rows("completion")


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize("first_result", ["NORMAL", "NG"])
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_original_completion_retry_stays_with_first_work_after_another_bin(
    manual_bins, first_result, direction
):
    flow = manual_bins
    await flow.ready_for_bin_work(direction=direction)
    originals = []
    for bin_code, scanned_at, completed_at, result in (
        ("A000000001", 10, 50, first_result),
        ("A000000002", 20, 60, "NORMAL"),
    ):
        assert (await flow.scan(bin_code + "-C", timestamp=scanned_at)).status_code == 200
        work = next(r for r in await flow.rows("manual_bin") if r["bin_code"] == bin_code)
        await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
        assert await flow.advance("bin", work["arrival_id"]) == "WORK_REQUIRED"
        raw = flow.completion(
            dict(task_id="T1", bin_code=bin_code, result=result, completed_at=completed_at)
        )
        assert (await flow.deliver(raw)).status_code == 202
        operation_id = json.loads(raw)["operation_id"]
        assert await flow.advance("completion", operation_id) == "APPLIED"
        released = next(
            r for r in await flow.rows("manual_bin") if r["arrival_id"] == work["arrival_id"]
        )
        assert released["state"] == "RELEASED" and released["command_code"]
        originals.append((raw, dict(released)))

    original_raw, original_work = originals[0]
    original_id = json.loads(original_raw)["operation_id"]
    original_notice = dict(
        next(r for r in await flow.rows("completion") if r["operation_id"] == original_id)
    )
    assert original_notice["arrival_id"] == original_work["arrival_id"]
    assert (await flow.scan(timestamp=30)).status_code == 200
    current = next(
        r
        for r in await flow.rows("manual_bin")
        if r["bin_code"] == "A000000001" and r["arrival_id"] != original_work["arrival_id"]
    )
    await flow.decision(dict(result="WORK_REQUIRED", task_id="T1"))
    assert await flow.advance("bin", current["arrival_id"]) == "WORK_REQUIRED"
    assert (await flow.deliver(original_raw)).body["code"] == "DUPLICATE"
    assert await flow.advance("completion", original_id) == "APPLIED"

    changed = json.loads(original_raw)
    changed["data"]["result"] = "NG" if first_result == "NORMAL" else "NORMAL"
    conflict = await flow.deliver(json.dumps(changed).encode())
    assert conflict.status_code == 409
    assert conflict.body["data"]["reason_code"] == "IDEMPOTENCY_CONFLICT"
    assert len(await flow.rows("scan2_release", component=True)) == 2
    assert (
        next(r for r in await flow.rows("completion") if r["operation_id"] == original_id)
        == original_notice
    )
    assert (
        next(
            r
            for r in await flow.rows("manual_bin")
            if r["arrival_id"] == original_work["arrival_id"]
        )
        == original_work
    )
    assert (
        next(r for r in await flow.rows("manual_bin") if r["arrival_id"] == current["arrival_id"])[
            "state"
        ]
        == "WORK_REQUIRED"
    )

    fresh = flow.completion(
        dict(task_id="T1", bin_code="A000000001", result=first_result, completed_at=80)
    )
    fresh_id = json.loads(fresh)["operation_id"]
    assert (await flow.deliver(fresh)).status_code == 202
    assert await flow.advance("completion", fresh_id) == "APPLIED"
    assert (await flow.deliver(fresh)).body["code"] == "DUPLICATE"
    assert await flow.advance("completion", fresh_id) == "APPLIED"
    assert len(await flow.rows("scan2_release", component=True)) == 3
    assert (
        next(r for r in await flow.rows("completion") if r["operation_id"] == fresh_id)[
            "arrival_id"
        ]
        == current["arrival_id"]
    )
    assert (
        next(
            r
            for r in await flow.rows("manual_bin")
            if r["arrival_id"] == original_work["arrival_id"]
        )
        == original_work
    )
