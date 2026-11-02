import asyncio

import pytest


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
async def test_wait_expiry_creates_one_original_evaluation(manual_bins):
    from app.businesses.smt_manual.manual_bin_flow import ManualBinFlow

    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    original = (await flow.rows("manual_bin"))[0]
    await flow.decision({"result": "WAIT", "retry_after_ms": 10})
    assert await flow.advance("bin", original["arrival_id"], now_ms=1000) == "WAIT"
    flow.manual = ManualBinFlow(
        flow.manual.capabilities, flow.roller, repository=flow.task.repository
    )
    assert (await flow.scan(timestamp=20)).status_code == 200
    assert await flow.advance("bin", original["arrival_id"], now_ms=1009) == "WAIT"
    assert len(await flow.rows("admission")) == 1
    assert await flow.advance("bin", original["arrival_id"], now_ms=1010) == "ADMISSION_PENDING"
    assert await flow.advance("bin", original["arrival_id"], now_ms=1010) == "ADMISSION_PENDING"
    evaluations = await flow.rows("admission")
    assert (
        len(evaluations) == 2 and evaluations[0]["operation_id"] != evaluations[1]["operation_id"]
    )
    saved = (await flow.rows("manual_bin"))[0]
    assert saved["arrival_id"] == original["arrival_id"] and saved["scanned_at"] == 10
    await flow.decision({"result": "NO_WORK"})
    assert await flow.advance("bin", original["arrival_id"], now_ms=2000) == "RELEASED"
    assert await flow.advance("bin", original["arrival_id"], now_ms=3000) == "RELEASED"
    assert len(await flow.rows("admission")) == 2


async def test_concurrent_wait_expiry_has_one_new_evaluation(
    manual_bins, overlapping_manual_snapshots
):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision({"result": "WAIT", "retry_after_ms": 1})
    await flow.advance("bin", arrival, now_ms=1000)
    await asyncio.gather(
        *(
            flow.advance("bin", arrival, now_ms=1001, before=overlapping_manual_snapshots.before)
            for _ in range(2)
        )
    )
    assert len(await flow.rows("admission")) == 2
    assert 1 in overlapping_manual_snapshots.attempts


async def test_pending_candidates_survive_reassembly_and_keep_each_original_record(manual_bins):
    from app.businesses.smt_manual.manual_bin_flow import ManualBinFlow

    flow = manual_bins
    await flow.ready()
    assert (await flow.scan()).status_code == 200
    assert (await flow.scan("A000000002-C", timestamp=20)).status_code == 200
    raw = flow.completion()
    assert (await flow.deliver(raw)).status_code == 202
    rebuilt = ManualBinFlow(flow.manual.capabilities, flow.roller, repository=flow.task.repository)
    pending = rebuilt.list_pending
    async with flow.factory() as session:
        upper = await rebuilt.get_pending_upper_id(session, kind="MANUAL_BIN")
        first = await pending(session, kind="MANUAL_BIN", after_id=None, upper_id=upper, limit=1)
        second = await pending(
            session, kind="MANUAL_BIN", after_id=first[0], upper_id=upper, limit=1
        )
        assert first + second == tuple(row["arrival_id"] for row in await flow.rows("manual_bin"))
        completion_upper = await rebuilt.get_pending_upper_id(session, kind="MANUAL_COMPLETION")
        notices = await pending(
            session, kind="MANUAL_COMPLETION", after_id=None, upper_id=completion_upper, limit=1
        )
        assert notices == ((await flow.rows("completion"))[0]["operation_id"],)
