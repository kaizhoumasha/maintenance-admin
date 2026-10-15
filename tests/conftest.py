import math
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("REDIS_URL", "redis://localhost:26379/0")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import get_session  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.session_service import _ISSUE_TOKENS, _REVOKE_ALL  # noqa: E402
from scripts.seed_admin import seed_admin  # noqa: E402


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("设置 TEST_DATABASE_URL 指向独立测试库后运行 PostgreSQL 测试")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "DATABASE_URL": url},
        check=True,
    )
    return url


@pytest.fixture
async def pg_sessions(pg_url):
    engine = create_async_engine(pg_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def real_redis():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("设置 TEST_REDIS_URL 指向独立测试实例后运行 Redis 测试")
    async with Redis.from_url(url, decode_responses=True) as instance:
        await instance.ping()
        yield instance


class FakeRedis:
    """只实现 auth 用到的命令;TTL 不模拟,只记录下来供断言。"""

    def __init__(self):
        self.data, self.ttl = {}, {}

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def sadd(self, key, member):
        self.data.setdefault(key, set()).add(member)

    async def smembers(self, key):
        return set(self.data.get(key, set()))

    async def srem(self, key, member):
        self.data.get(key, set()).discard(member)

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def eval(self, script, numkeys, *args):
        """单元测试模拟脚本行为;原子性及过期由真实 Redis 集成测试覆盖。"""
        key = args[0]
        if script == _REVOKE_ALL:
            members = await self.smembers(key)
            await self.delete(key, *(f"refresh:{jti}" for jti in members))
            return 1
        if script == _ISSUE_TOKENS:
            _, token_key, sub, ttl, jti, old, multi = args
            if old:
                if self.data.get(f"refresh:{old}") != sub:
                    return 0
                await self.delete(f"refresh:{old}")
                await self.srem(key, old)
            for member in await self.smembers(key):
                if not multi:
                    await self.delete(f"refresh:{member}")
                if not multi or f"refresh:{member}" not in self.data:
                    await self.srem(key, member)
            await self.set(token_key, sub, ex=ttl)
            await self.sadd(key, jti)
            await self.expire(key, ttl)
            return 1
        key, window, limit, member = args
        now, zset = time.time() * 1000, self.data.setdefault(key, {})
        for m in [m for m, s in zset.items() if s <= now - window * 1000]:
            del zset[m]
        if len(zset) >= limit:
            return [0, math.ceil((min(zset.values()) + window * 1000 - now) / 1000)]
        zset[member] = now
        return [1, 0]


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    # 内存 SQLite 隔离测试；StaticPool 让所有连接共享同一个内存库
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    app.dependency_overrides[get_redis] = lambda: redis
    app.state.audit_session_factory = factory
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
async def admin(client):
    """种子化 admin 用户(id=1,拥有全部权限)并返回其 Authorization 头。"""
    gen = app.dependency_overrides[get_session]()
    session = await anext(gen)
    await seed_admin(session, "root", "root@example.com", "password123")
    await gen.aclose()
    r = await client.post(
        "/api/v1/auth/login", json={"username": "root", "password": "password123"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
