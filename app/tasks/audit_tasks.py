from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import get_settings
from app.services import audit_service
from app.tasks._db import run_with_factory
from app.tasks.celery_app import celery_app


async def run_archive(factory: async_sessionmaker[AsyncSession], days: int) -> int:
    async with factory() as session:
        return await audit_service.archive_old(session, days)


@celery_app.task(name="audit.archive_old")
def archive_old() -> int:
    settings = get_settings()
    return run_with_factory(
        settings.DATABASE_URL, lambda f: run_archive(f, settings.AUDIT_RETENTION_DAYS)
    )
