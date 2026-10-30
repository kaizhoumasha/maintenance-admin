import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import create_database_engine


def run_with_factory[T](
    database_url: str, work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]
) -> T:
    """Celery 任务的数据库入口:每次 asyncio.run 都是新事件循环,不能复用绑定在 FastAPI 循环上的
    全局 engine/连接池。每个任务建立自己的引擎,在本任务内复用连接,并在同一循环释放。"""

    async def execute() -> T:
        engine = create_database_engine(database_url, hide_parameters=True)
        try:
            return await work(async_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())
