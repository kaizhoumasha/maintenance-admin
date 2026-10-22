from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import CallbackReceipt, CallbackSnapshot
from app.tasks.callback_tasks import run_cleanup


async def test_cleanup_only_removes_expired_snapshots(db):
    now = datetime.now(UTC)
    db.add(
        CallbackReceipt(
            operation="unknown@v1",
            operation_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
            raw_digest="a" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=422,
            body={"code": "REJECTED"},
            resp_timestamp=1,
            created_at=now - timedelta(days=30),
        )
    )
    for age in (8, 7, 6):
        db.add(
            CallbackSnapshot(
                request_id=str(age),
                received_at=now - timedelta(days=age),
                body_b64="",
                truncated=False,
                http_status=422,
                response_body=None,
                outcome="rejected",
            )
        )
    await db.commit()
    factory = async_sessionmaker(db.bind)
    assert await run_cleanup(factory, 7, now=now, batch=1) == 1
    assert set(await db.scalars(select(CallbackSnapshot.request_id))) == {"7", "6"}
    assert len(list(await db.scalars(select(CallbackReceipt)))) == 1


@pytest.mark.parametrize("days", [0, -1, True])
async def test_cleanup_rejects_invalid_retention(db, days):
    with pytest.raises((TypeError, ValueError)):
        await run_cleanup(None, days)


async def test_receipt_cleanup_removes_only_receipts_older_than_the_retention(db):
    from app.tasks.callback_tasks import run_receipt_cleanup

    now = datetime.now(UTC)
    for age in (91, 89):
        db.add(
            CallbackReceipt(
                operation="unknown@v1",
                operation_id=f"01a13b00-cabd-7390-890a-0c093e7c07a{age % 10}",
                raw_digest="a" * 64,
                effective_digest=None,
                compare_mode="RAW",
                status_code=422,
                body={"code": "REJECTED"},
                resp_timestamp=1,
                created_at=now - timedelta(days=age),
            )
        )
    await db.commit()
    factory = async_sessionmaker(db.bind)
    assert await run_receipt_cleanup(factory, 90, now=now, batch=1) == 1
    remaining = list(await db.scalars(select(CallbackReceipt)))
    assert [r.operation_id[-1] for r in remaining] == ["9"]
