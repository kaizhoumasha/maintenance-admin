import pytest


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_both_manual_directions_use_bin_admission(manual_bins, direction):
    import json

    from sqlalchemy import select

    from app.models import ReliableMessage

    flow = manual_bins
    await flow.ready(direction=direction)
    response = await flow.scan()
    assert response.status_code == 200
    work = (await flow.rows("manual_bin"))[0]
    assert work["raw_bin_code"] == "A000000001-C"
    assert work["bin_code"] == "A000000001" and work["task_id"] == "T1"
    async with flow.factory() as session:
        message = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.operation == "manual_bin.work_admission_decide@v1"
            )
        )
        assert json.loads(message.request_raw)["data"] == {
            "task_id": "T1",
            "bin_code": "A000000001",
            "scanned_at": 10,
        }
        assert message.business_key == "business:smt_manual"
    assert not await flow.rows("scan2_release", component=True)


@pytest.mark.parametrize(
    "raw,ready", [("NOREAD", True), ("A000000001-B", True), ("A000000001-C", False)]
)
async def test_invalid_scan_or_missing_context_keeps_arrival(manual_bins, raw, ready):
    flow = manual_bins
    if ready:
        await flow.ready()
    assert (await flow.scan(raw)).status_code == 200
    assert len(await flow.rows("scan_fact", component=True)) == 1
    assert len(await flow.rows("manual_bin")) == 1
    assert not await flow.rows("admission")
    assert not await flow.rows("scan2_release", component=True)


async def test_scan2_acceptance_rolls_back_with_original_receiver(manual_bins):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan(rollback=True)).status_code == 200
    assert not await flow.rows("manual_bin")
    assert not await flow.rows("admission")
    assert not await flow.rows("scan_fact", component=True)


async def test_future_scan_keeps_original_fact_without_request(manual_bins):
    flow = manual_bins
    await flow.ready()
    assert (await flow.scan(timestamp=2**63 - 1)).status_code == 200
    work = (await flow.rows("manual_bin"))[0]
    assert work["scanned_at"] == 2**63 - 1 and work["state"] == "HOLD"
    assert not await flow.rows("admission")
    assert not await flow.rows("scan2_release", component=True)
