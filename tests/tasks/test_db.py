import os
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.models import Base, User

PG_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://admin:admin123@localhost:25432/maintenance",
)


async def test_pg_smoke() -> None:
    """冒烟：asyncpg 链路通 → 建表 → 写读一次 → 清理。PG 没起则跳过。"""
    engine = create_async_engine(PG_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres(25432) 未启动，先 docker-compose up -d")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        username = f"smoke_{uuid.uuid4().hex[:8]}"
        async with factory() as session:
            session.add(User(username=username, email=f"{username}@e.com", hashed_password="x"))
            await session.commit()
        async with factory() as session:
            user = await session.scalar(select(User).where(User.username == username))
            assert user is not None and user.email == f"{username}@e.com"
        async with factory() as session:
            await session.execute(delete(User).where(User.username == username))
            await session.commit()
    finally:
        await engine.dispose()
