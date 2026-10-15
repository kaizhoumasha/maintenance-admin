import uuid

from sqlalchemy import delete, select

from app.models import User


async def test_pg_smoke(pg_sessions) -> None:
    """显式指定独立测试库后,经真实迁移建表并验证 asyncpg 写读链路。"""
    username = f"smoke_{uuid.uuid4().hex[:8]}"
    try:
        async with pg_sessions() as session:
            session.add(User(username=username, email=f"{username}@e.com", hashed_password="x"))
            await session.commit()
        async with pg_sessions() as session:
            user = await session.scalar(select(User).where(User.username == username))
            assert user is not None and user.email == f"{username}@e.com"
    finally:
        async with pg_sessions() as session:
            await session.execute(delete(User).where(User.username == username))
            await session.commit()
