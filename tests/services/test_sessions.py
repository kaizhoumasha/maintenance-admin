import asyncio
import uuid

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.redis import get_redis
from app.core.security import decode_token
from app.main import app
from app.models import User
from app.services import session_service
from tests.controllers.test_auth import LOGIN, _login, _refresh


async def test_refresh_cannot_restore_session_after_logout_all(client, admin, real_redis):
    reached, resume = asyncio.Event(), asyncio.Event()

    class PausedRedis:
        def __getattr__(self, name):
            return getattr(real_redis, name)

        async def eval(self, script, numkeys, *args):
            result = await real_redis.eval(script, numkeys, *args)
            if any(str(key).startswith("user_refresh:") for key in args[:numkeys]):
                reached.set()
                await asyncio.wait_for(resume.wait(), 5)
            return result

    app.dependency_overrides[get_redis] = lambda: real_redis
    access, token = await _login(client, LOGIN)
    app.dependency_overrides[get_redis] = lambda: PausedRedis()
    pending = asyncio.create_task(_refresh(client, token))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        app.dependency_overrides[get_redis] = lambda: real_redis
        response = await client.post(
            "/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {access}"}
        )
        assert response.status_code == 204
        resume.set()
        refreshed = await asyncio.wait_for(pending, 5)
        assert refreshed.status_code == 200
        assert (await _refresh(client, refreshed.cookies.get("refresh_token"))).status_code == 401
    finally:
        resume.set()
        await asyncio.gather(pending, return_exceptions=True)
        await session_service.revoke_all(real_redis, "1")


async def test_issuing_tokens_prunes_expired_session_members(real_redis):
    user = User(id=uuid.uuid4().int, allow_multi_login=True)
    key = f"user_refresh:{user.id}"
    try:
        _, token = await session_service.issue_tokens(real_redis, user)
        jti = decode_token(token, "refresh")["jti"]
        await real_redis.pexpire(f"refresh:{jti}", 1)
        await asyncio.sleep(0.01)
        assert not await real_redis.exists(f"refresh:{jti}")
        _, token = await session_service.issue_tokens(real_redis, user)
        current = decode_token(token, "refresh")["jti"]
        assert await real_redis.smembers(key) == {current}
    finally:
        await session_service.revoke_all(real_redis, str(user.id))


@pytest.mark.parametrize("multi, count", [(False, 1), (True, 2)])
async def test_concurrent_logins_preserve_login_policy(real_redis, multi, count):
    user = User(id=uuid.uuid4().int, allow_multi_login=multi)
    try:
        tokens = await asyncio.gather(
            *(session_service.issue_tokens(real_redis, user) for _ in range(2))
        )
        members = await real_redis.smembers(f"user_refresh:{user.id}")
        assert len(members) == count
        live = [
            bool(await real_redis.exists(f"refresh:{decode_token(token, 'refresh')['jti']}"))
            for _, token in tokens
        ]
        assert sum(live) == count
    finally:
        await session_service.revoke_all(real_redis, str(user.id))


async def test_concurrent_refresh_replay_succeeds_once(real_redis):
    user = User(id=uuid.uuid4().int, allow_multi_login=True)
    try:
        _, token = await session_service.issue_tokens(real_redis, user)
        jti = decode_token(token, "refresh")["jti"]
        results = await asyncio.gather(
            *(session_service.issue_tokens(real_redis, user, jti) for _ in range(2)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, tuple) for result in results) == 1
        errors = [result for result in results if isinstance(result, AppError)]
        assert len(errors) == 1 and errors[0].code == ErrorCode.UNAUTHORIZED
        assert not await real_redis.exists(f"refresh:{jti}")
    finally:
        await session_service.revoke_all(real_redis, str(user.id))
