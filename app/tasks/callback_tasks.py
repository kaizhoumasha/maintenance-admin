import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import get_settings
from app.models import CallbackSnapshot
from app.reliable.config import positive
from app.tasks.celery_app import celery_app


async def run_cleanup(factory, days, *, now=None, batch=500) -> int:
    positive(days, "CALLBACK_SNAPSHOT_RETENTION_DAYS")
    positive(batch, "batch")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    total = 0
    while True:
        async with factory() as session, session.begin():
            expired = (
                select(CallbackSnapshot.id)
                .where(CallbackSnapshot.received_at < cutoff)
                .order_by(CallbackSnapshot.received_at, CallbackSnapshot.id)
                .limit(batch)
            )
            removed = (
                await session.execute(
                    delete(CallbackSnapshot).where(CallbackSnapshot.id.in_(expired))
                )
            ).rowcount
        total += removed
        if removed < batch:
            return total


@celery_app.task(name="callback.cleanup_snapshots")
def cleanup_snapshots() -> int:
    settings = get_settings()

    async def execute():
        engine = create_async_engine(
            settings.DATABASE_URL, poolclass=NullPool, hide_parameters=True
        )
        try:
            return await run_cleanup(
                async_sessionmaker(engine), settings.CALLBACK_SNAPSHOT_RETENTION_DAYS
            )
        finally:
            await engine.dispose()

    return asyncio.run(execute())
