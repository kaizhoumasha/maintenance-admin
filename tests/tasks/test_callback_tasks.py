from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.integrations.callback_receipts import insert_receipt
from app.integrations.wms_callback.receiver import CallbackResponse
from app.integrations.wms_callback.snapshots import record_snapshot
from app.models import IntegrationExchange, IntegrationRecord
from app.tasks.callback_tasks import run_cleanup


async def test_cleanup_only_purges_expired_diagnostic_bodies(db):
    now = datetime.now(UTC)
    await insert_receipt(
        db,
        operation="unknown@v1",
        operation_id="original",
        raw_digest="a" * 64,
        effective_digest=None,
        compare_mode="RAW",
        status_code=422,
        body={"code": "REJECTED"},
        resp_timestamp=1,
        created_at=now - timedelta(days=30),
    )
    await db.commit()
    factory = async_sessionmaker(db.bind)
    for age in (8, 7, 6):
        await record_snapshot(
            factory,
            preview=b"body",
            truncated=False,
            response=CallbackResponse(422, {"code": "REJECTED"}, "rejected"),
            request_id=str(age),
            received_at=now - timedelta(days=age),
        )
    assert await run_cleanup(factory, 7, now=now, batch=1) == 1
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(IntegrationRecord).where(
                    IntegrationRecord.evidence_class == "DIAGNOSTIC",
                    IntegrationRecord.record_kind == "CALLBACK_RECEIVED",
                )
            )
        )
        assert {row.details["request_id"] for row in rows if row.body_state != "PURGED"} == {
            "7",
            "6",
        }
        assert len(rows) == 3
        assert (
            len(
                list(
                    await session.scalars(
                        select(IntegrationExchange).where(
                            IntegrationExchange.role == "RECEIPT",
                            IntegrationExchange.receipt_active.is_(True),
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("days", [0, -1, True])
async def test_cleanup_rejects_invalid_retention(db, days):
    with pytest.raises((TypeError, ValueError)):
        await run_cleanup(None, days)


async def test_receipt_cleanup_releases_only_expired_acceptance_slots(db):
    from app.tasks.callback_tasks import run_receipt_cleanup

    now = datetime.now(UTC)
    for age in (91, 89):
        await insert_receipt(
            db,
            operation="unknown@v1",
            operation_id=str(age),
            raw_digest="a" * 64,
            effective_digest=None,
            compare_mode="RAW",
            status_code=422,
            body={"code": "REJECTED"},
            resp_timestamp=1,
            created_at=now - timedelta(days=age),
        )
    await db.commit()
    factory = async_sessionmaker(db.bind)
    assert await run_receipt_cleanup(factory, 90, now=now, batch=1) == 1
    async with factory() as session:
        roots = list(
            await session.scalars(
                select(IntegrationExchange).where(IntegrationExchange.role == "RECEIPT")
            )
        )
        assert [r.operation_id for r in roots if r.receipt_active] == ["89"]
        assert len(roots) == 2
