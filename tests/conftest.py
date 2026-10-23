import asyncio
import math
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("REDIS_URL", "redis://localhost:26379/0")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import delete, exists, func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import hash_password  # noqa: E402
from app.core.database import get_session  # noqa: E402
from app.core.identifiers import uuid7
from app.core.redis import get_redis  # noqa: E402
from app.database import create_database_engine as create_async_engine  # noqa: E402
from app.integrations.wms_callback import snapshots  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    AuditLogArchive,
    Base,
    CallbackReceipt,
    CallbackSnapshot,
    Menu,
    Permission,
    ReliableMessage,
    Role,
    TransportTask,
    User,
    role_permission,
)
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


# WMS 回调相关的共享存储夹具:SQLite(create_all)与独立 PostgreSQL 各跑一遍,回调路由改用它的会话工厂
@pytest.fixture(params=["sqlite", "postgres"])
async def callback_db(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        engine = create_async_engine(request.getfixturevalue("pg_url"))
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'callback.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app.state, "callback_session_factory", factory, raising=False)
    try:
        yield factory
    finally:
        if request.param == "postgres":
            async with factory.begin() as session:
                await session.execute(delete(CallbackSnapshot))
                await session.execute(delete(CallbackReceipt))
                await session.execute(delete(ReliableMessage))  # Transport 回调测试造的任务与消息
                await session.execute(delete(TransportTask))
        await engine.dispose()


# 回调接收合同测试用:另外保证回调路径不碰 Redis 限流,并重置快照计数
@pytest.fixture
async def callback_store(callback_db, monkeypatch, redis):
    monkeypatch.setattr(snapshots, "_written", 0)
    monkeypatch.setattr(snapshots, "_dropped", 0)
    monkeypatch.setattr(snapshots, "_window_start", 0.0)

    async def forbidden(*args, **kwargs):
        raise AssertionError("callback must not access Redis rate limiting")

    monkeypatch.setattr(redis, "eval", forbidden)
    return callback_db


@pytest.fixture
async def real_redis():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("设置 TEST_REDIS_URL 指向独立测试实例后运行 Redis 测试")
    async with Redis.from_url(url, decode_responses=True) as instance:
        await instance.ping()
        yield instance


@pytest.fixture
def wait_for_lock(pg_sessions):
    """并发测试用:轮询 pg_stat_activity,等到有会话在等锁(给 pid 则只看它)或 pending 已结束。"""

    async def wait(pending, pid=None):
        where = "pid = :pid" if pid else "datname = current_database()"
        query = text(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
            f"WHERE wait_event_type = 'Lock' AND {where})"
        )
        async with pg_sessions() as observer, asyncio.timeout(5):
            while not pending.done():
                if await observer.scalar(query, {"pid": pid} if pid else {}):
                    return
                await (
                    observer.rollback()
                )  # pg_stat_activity 在事务内是快照,必须结束事务才能看到新状态
                await asyncio.sleep(0.01)

    return wait


class FakeRedis:
    """只实现 auth 用到的命令;TTL 不模拟,只记录下来供断言。"""

    def __init__(self):
        self.data, self.ttl = {}, {}

    async def ping(self):
        return True

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
async def db():
    """service 层测试用的独立会话(内存 SQLite)。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


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


@pytest.fixture
async def pg_client(pg_sessions, redis):
    """自建最小授权数据,清理后检查公共 RBAC 数据未变;不运行生产种子。"""
    suffix = uuid7().hex
    users, roles, created_permissions = [], [], []
    overrides = app.dependency_overrides.copy()
    audit_factory = app.state.audit_session_factory

    async def snapshot(session):
        admin_permissions = set(
            await session.scalars(
                select(role_permission.c.permission_id).join(Role).where(Role.name == "admin")
            )
        )
        return (
            admin_permissions,
            await session.scalar(select(func.count()).select_from(Permission)),
            await session.scalar(select(func.count()).select_from(Menu)),
        )

    async def sessions():
        async with pg_sessions() as session:
            yield session

    async with pg_sessions() as session:
        before = await snapshot(session)
    try:
        async with pg_sessions() as session:
            permissions = []
            for code in ("admin:user:list", "admin:audit:read", "admin:audit:read_archive"):
                permission = await session.scalar(select(Permission).where(Permission.code == code))
                if permission is None:
                    permission = Permission(code=code, name=code)
                    session.add(permission)
                    await session.flush()
                    created_permissions.append(permission.id)
                permissions.append(permission)
            role = Role(name=suffix, permissions=permissions)
            roles.append(role)
            password_hash = await asyncio.to_thread(hash_password, "password123")
            for prefix, assigned_roles in (("reader", [role]), ("denied", [])):
                user = User(
                    username=f"{prefix}_{suffix}",
                    email=f"{prefix}_{suffix}@example.test",
                    hashed_password=password_hash,
                    roles=assigned_roles,
                )
                users.append(user)
                session.add(user)
            await session.commit()
        app.dependency_overrides[get_session] = sessions
        app.dependency_overrides[get_redis] = lambda: redis
        app.state.audit_session_factory = pg_sessions
        transport = ASGITransport(app=app, client=(suffix, 123), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = []
            for user in users:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": user.username,
                        "password": "password123",
                    },
                )
                assert response.status_code == 200, response.text
                headers.append({"Authorization": f"Bearer {response.json()['access_token']}"})
            yield {
                "client": client,
                "headers": headers[0],
                "denied": headers[1],
                "tag": suffix,
                "users": users,
                "roles": roles,
            }
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        app.state.audit_session_factory = audit_factory
        async with pg_sessions() as session:
            for model in (AuditLog, AuditLogArchive):
                await session.execute(delete(model).where(model.resource == suffix))
            await session.execute(delete(User).where(User.id.in_([u.id for u in users if u.id])))
            await session.execute(delete(Role).where(Role.id.in_([r.id for r in roles if r.id])))
            await session.execute(
                delete(Permission).where(
                    Permission.id.in_(created_permissions),
                    ~exists().where(role_permission.c.permission_id == Permission.id),
                )
            )
            await session.commit()
            assert await snapshot(session) == before
        assert app.dependency_overrides == overrides
        assert app.state.audit_session_factory is audit_factory
