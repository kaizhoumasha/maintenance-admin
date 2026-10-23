from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.database import create_database_engine as create_async_engine

# pool_pre_ping:取连接前探测,数据库重启/空闲连接被回收后丢弃失效连接,而不是让首个请求报错
engine = create_async_engine(get_settings().DATABASE_URL, hide_parameters=True, pool_pre_ping=True)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
