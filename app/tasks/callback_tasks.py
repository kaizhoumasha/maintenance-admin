import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core import get_settings
from app.database import create_database_engine as create_async_engine
from app.models import CallbackReceipt, CallbackSnapshot
from app.reliable.config import positive
from app.tasks.celery_app import celery_app


async def _purge(factory, model, column, days, name, now, batch) -> int:
    """分批删除 `column` 早于保留期的行,每批独立提交。"""
    positive(days, name)
    positive(batch, "batch")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    total = 0
    while True:
        async with factory() as session, session.begin():
            expired = (
                select(model.id).where(column < cutoff).order_by(column, model.id).limit(batch)
            )
            removed = (await session.execute(delete(model).where(model.id.in_(expired)))).rowcount
        total += removed
        if removed < batch:
            return total


async def run_cleanup(factory, days, *, now=None, batch=500) -> int:
    return await _purge(
        factory,
        CallbackSnapshot,
        CallbackSnapshot.received_at,
        days,
        "CALLBACK_SNAPSHOT_RETENTION_DAYS",
        now,
        batch,
    )


async def run_receipt_cleanup(factory, days, *, now=None, batch=500) -> int:
    """收据保留期与可靠层终态消息一致(`RELIABLE_RETENTION_DAYS`);超期重放的旧身份按新消息处理,
    业务幂等由任务行上的版本与摘要承担。"""
    return await _purge(
        factory,
        CallbackReceipt,
        CallbackReceipt.created_at,
        days,
        "RELIABLE_RETENTION_DAYS",
        now,
        batch,
    )


def _run(cleanup, days_setting: str) -> int:
    settings = get_settings()

    async def execute():
        engine = create_async_engine(
            settings.DATABASE_URL, poolclass=NullPool, hide_parameters=True
        )
        try:
            return await cleanup(async_sessionmaker(engine), getattr(settings, days_setting))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


@celery_app.task(name="callback.cleanup_snapshots")
def cleanup_snapshots() -> int:
    return _run(run_cleanup, "CALLBACK_SNAPSHOT_RETENTION_DAYS")


@celery_app.task(name="callback.cleanup_receipts")
def cleanup_receipts() -> int:
    return _run(run_receipt_cleanup, "RELIABLE_RETENTION_DAYS")
