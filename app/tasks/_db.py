import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.database import create_database_engine


def run_with_factory[T](
    database_url: str, work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]
) -> T:
    """Celery 任务的数据库入口:每次 asyncio.run 都是新事件循环,不能复用绑定在 FastAPI 循环上的
    全局 engine/连接池,所以用 NullPool 建一次性引擎,并在同一个循环里释放。"""

    async def execute() -> T:
        engine = create_database_engine(database_url, poolclass=NullPool, hide_parameters=True)
        try:
            return await work(async_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())
