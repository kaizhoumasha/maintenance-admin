import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core import get_settings
from app.database import create_database_engine as create_async_engine
from app.services import audit_service
from app.tasks.celery_app import celery_app


async def run_archive(factory: async_sessionmaker[AsyncSession], days: int) -> int:
    async with factory() as session:
        return await audit_service.archive_old(session, days)


@celery_app.task(name="audit.archive_old")
def archive_old() -> int:
    settings = get_settings()
    # 每次 asyncio.run 都是新事件循环,不能复用绑定在 FastAPI 循环上的全局 engine/连接池
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool, hide_parameters=True)
    try:
        return asyncio.run(run_archive(async_sessionmaker(engine), settings.AUDIT_RETENTION_DAYS))
    finally:
        asyncio.run(engine.dispose())
