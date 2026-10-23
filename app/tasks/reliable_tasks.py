from datetime import UTC, datetime

from app.core import get_settings
from app.reliable.store import CLEANUP_BATCH_SIZE
from app.reliable.store import cleanup_terminal as delete_terminal
from app.tasks._db import run_with_factory
from app.tasks.celery_app import celery_app


async def run_cleanup(factory, days, *, now=None, batch=CLEANUP_BATCH_SIZE) -> int:
    now = now or datetime.now(UTC)
    total = 0
    while True:
        # 每批独立提交,避免一次事务删除海量带大字段的行。
        async with factory() as session, session.begin():
            deleted = await delete_terminal(session, now=now, retention_days=days, limit=batch)
        total += deleted
        if deleted < batch:
            return total


@celery_app.task(name="reliable.cleanup_terminal")
def cleanup_terminal() -> int:
    settings = get_settings()
    return run_with_factory(
        settings.DATABASE_URL, lambda f: run_cleanup(f, settings.RELIABLE_RETENTION_DAYS)
    )
