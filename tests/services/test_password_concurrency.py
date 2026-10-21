import asyncio
import threading

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core import hash_password
from app.core.database import get_session
from app.core.identifiers import uuid4
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.main import app
from app.models import User
from app.services import session_service, user_service


@pytest.fixture
async def account(pg_sessions, real_redis):
    suffix = uuid4().hex
    async with pg_sessions() as session:
        user = User(
            username=suffix,
            email=f"{suffix}@example.test",
            hashed_password=await asyncio.to_thread(hash_password, "old-password-123"),
            allow_multi_login=True,
        )
        session.add(user)
        await session.commit()

    async def sessions():
        async with pg_sessions() as session:
            yield session

    overrides = app.dependency_overrides.copy()
    audit_factory = app.state.audit_session_factory
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_redis] = lambda: real_redis
    app.state.audit_session_factory = pg_sessions
    try:
        # 每个用例独立的客户端地址,避免真实 Redis 的限流计数串扰。
        transport = ASGITransport(app=app, client=(suffix, 123), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield user, client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        app.state.audit_session_factory = audit_factory
        await session_service.revoke_all(real_redis, str(user.id))
        async with pg_sessions() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


async def _change(pg_sessions, redis, user_id, reset):
    async with pg_sessions() as session:
        if reset:
            await user_service.reset_password(session, redis, user_id, "new-password-456")
        else:
            user = await session.get(User, user_id)
            await user_service.change_password(
                session, redis, user, "old-password-123", "new-password-456"
            )


@pytest.mark.parametrize("reset", [False, True])
async def test_login_waits_for_password_commit(
    account, pg_sessions, real_redis, wait_for_lock, monkeypatch, reset
):
    user, client = account
    reached, release = asyncio.Event(), asyncio.Event()
    revoke = session_service.revoke_all

    async def pause_after_revoke(redis, user_id):
        await revoke(redis, user_id)
        reached.set()
        await release.wait()

    monkeypatch.setattr(session_service, "revoke_all", pause_after_revoke)
    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        group.create_task(_change(pg_sessions, real_redis, user.id, reset))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            login = group.create_task(
                client.post(
                    "/api/v1/auth/login",
                    json={"username": user.username, "password": "old-password-123"},
                )
            )
            await wait_for_lock(login)
        finally:
            release.set()
    assert login.result().status_code == 401
    assert not await real_redis.smembers(f"user_refresh:{user.id}")
    response = await client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": "new-password-456"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("operation", ["login", "refresh"])
async def test_password_change_waits_for_token_issuance(
    account, pg_sessions, real_redis, wait_for_lock, monkeypatch, reset, operation
):
    user, client = account
    credentials = {"username": user.username, "password": "old-password-123"}
    if operation == "refresh":
        assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 200
    reached, release = asyncio.Event(), asyncio.Event()
    issue = session_service.issue_tokens

    async def pause_before_issue(redis, user, refresh_jti=None):
        reached.set()
        await release.wait()
        return await issue(redis, user, refresh_jti)

    monkeypatch.setattr(session_service, "issue_tokens", pause_before_issue)
    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        request = (
            client.post("/api/v1/auth/login", json=credentials)
            if operation == "login"
            else client.post("/api/v1/auth/refresh")
        )
        login = group.create_task(request)
        try:
            await asyncio.wait_for(reached.wait(), 5)
            change = group.create_task(_change(pg_sessions, real_redis, user.id, reset))
            await wait_for_lock(change)
        finally:
            release.set()
    assert login.result().status_code == 200
    assert not await real_redis.smembers(f"user_refresh:{user.id}")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_waiting_password_change_rechecks_old_password(
    account, pg_sessions, real_redis, wait_for_lock, monkeypatch
):
    user, client = account
    reached, release = asyncio.Event(), asyncio.Event()
    revoke = session_service.revoke_all

    async def pause_first_revoke(redis, user_id):
        await revoke(redis, user_id)
        if not reached.is_set():
            reached.set()
            await release.wait()

    monkeypatch.setattr(session_service, "revoke_all", pause_first_revoke)
    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        group.create_task(_change(pg_sessions, real_redis, user.id, True))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            change = group.create_task(
                client.post(
                    "/api/v1/users/me/password",
                    headers={"Authorization": f"Bearer {create_access_token(str(user.id))}"},
                    json={"old_password": "old-password-123", "new_password": "other-password-789"},
                )
            )
            await wait_for_lock(change)
        finally:
            release.set()
    assert change.result().status_code == 400
    response = await client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": "new-password-456"}
    )
    assert response.status_code == 200


async def test_wrong_password_does_not_wait_for_user_lock(
    account, pg_sessions, real_redis, wait_for_lock
):
    user, client = account
    async with pg_sessions() as writer:
        await writer.get(User, user.id, with_for_update=True)
        pending = asyncio.create_task(
            client.post(
                "/api/v1/auth/login",
                json={"username": user.username, "password": "wrong-password"},
            )
        )
        try:
            await wait_for_lock(pending)
            assert pending.done(), "错误密码登录不应等待用户行锁"
            assert pending.result().status_code == 401
        finally:
            await writer.rollback()
            await asyncio.wait_for(pending, 5)
    assert not await real_redis.smembers(f"user_refresh:{user.id}")


@pytest.mark.parametrize("mutation", ["password", "deactivate"])
async def test_login_rechecks_user_after_unlocked_verification(
    account, pg_sessions, real_redis, wait_for_lock, monkeypatch, mutation
):
    user, client = account
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    verify = user_service.verify_password

    def pause_after_verify(password, password_hash):
        valid = verify(password, password_hash)
        loop.call_soon_threadsafe(reached.set)
        if not release.wait(5):
            raise TimeoutError("等待释放登录验密屏障超时")
        return valid

    async def mutate_user():
        if mutation == "password":
            await _change(pg_sessions, real_redis, user.id, True)
        else:
            async with pg_sessions() as session:
                current = await session.get(User, user.id)
                current.is_active = False
                await session.commit()

    monkeypatch.setattr(user_service, "verify_password", pause_after_verify)
    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        login = group.create_task(
            client.post(
                "/api/v1/auth/login",
                json={"username": user.username, "password": "old-password-123"},
            )
        )
        try:
            await asyncio.wait_for(reached.wait(), 5)
            change = group.create_task(mutate_user())
            await wait_for_lock(change)
            changed_before_lock = change.done()
        finally:
            release.set()
    assert changed_before_lock, "锁外验密不应阻塞改密或停用用户"
    response = login.result()
    assert response.status_code == 401 and response.json()["code"] == "UNAUTHORIZED"
    assert not await real_redis.smembers(f"user_refresh:{user.id}")
