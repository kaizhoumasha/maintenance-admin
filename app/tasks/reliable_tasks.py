from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import get_settings
from app.models import ReliableMessage
from app.reliable.config import positive
from app.reliable.store import CLEANUP_BATCH_SIZE, TERMINAL
from app.reliable.store import cleanup_terminal as delete_terminal
from app.tasks._db import run_with_factory
from app.tasks.celery_app import celery_app


async def run_cleanup(factory, days, *, now=None, batch=CLEANUP_BATCH_SIZE) -> int:
    positive(days, "retention_days")
    from app.tasks.callback_tasks import history_cleanup_ready

    if not await history_cleanup_ready(factory):
        return 0
    now = now or datetime.now(UTC)
    from app.businesses._components.bin_roller.retention import (
        cleanup_closed,
        protected_message_ids,
    )
    from app.businesses.coarse_sorting.retention import wms_message_references as coarse_references
    from app.businesses.smt_auto.retention import wms_message_references as auto_references

    async def wms_message_references(session, **scope):
        return (
            *await auto_references(session, **scope),
            *await coarse_references(session, **scope),
        )

    while True:
        async with factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "READ COMMITTED"})
            removed = await cleanup_closed(session, cutoff=now - timedelta(days=days), limit=batch)
        if removed < batch:
            break

    total, cursor = 0, 0
    while True:
        async with factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "READ COMMITTED"})
            ids = tuple(
                await session.scalars(
                    select(ReliableMessage.id)
                    .where(
                        ReliableMessage.id > cursor,
                        ReliableMessage.status.in_(TERMINAL),
                        ReliableMessage.updated_at < now - timedelta(days=days),
                    )
                    .order_by(ReliableMessage.id)
                    .limit(batch)
                )
            )
            if not ids:
                return total
            protected = await protected_message_ids(
                session, candidate_ids=ids, business_references=wms_message_references
            )
            total += await delete_terminal(
                session, now=now, retention_days=days, protected_ids=protected, candidate_ids=ids
            )
            cursor = ids[-1]


@celery_app.task(name="reliable.cleanup_terminal")
def cleanup_terminal() -> int:
    settings = get_settings()
    return run_with_factory(
        settings.DATABASE_URL, lambda f: run_cleanup(f, settings.RELIABLE_RETENTION_DAYS)
    )
