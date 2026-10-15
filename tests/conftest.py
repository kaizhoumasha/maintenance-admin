import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("REDIS_URL", "redis://localhost:26379/0")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.database import get_session  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from scripts.seed_admin import seed_admin  # noqa: E402


class FakeRedis:
    """只实现 auth 用到的命令;TTL 不模拟,只记录下来供断言。"""

    def __init__(self):
        self.data, self.ttl = {}, {}

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def getdel(self, key):
        return self.data.pop(key, None)

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

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """记录命令,execute 时依次执行;不模拟 MULTI 的原子性。"""

    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))

        return queue

    async def execute(self):
        for name, args, kwargs in self.ops:
            await getattr(self.redis, name)(*args, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


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
