import jwt
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import get_settings
from app.core.database import get_session
from app.core.redis import get_redis
from app.core.security import ALGORITHM, create_access_token
from app.main import app
from app.models import User

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}
LOGIN = {"username": "root", "password": "password123"}
KAI = {"username": "kai", "password": "password123"}


async def test_login_then_me(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post("/api/v1/auth/login", json={"username": "kai", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "kai"
    assert r.json()["allow_multi_login"] is False  # 普通用户默认单点


async def test_create_user_can_set_allow_multi_login(client, admin):
    r = await client.post(
        "/api/v1/users", json={**PAYLOAD, "allow_multi_login": True}, headers=admin
    )
    assert r.status_code == 201 and r.json()["allow_multi_login"] is True


async def test_bad_credentials_and_token(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post("/api/v1/auth/login", json={"username": "kai", "password": "wrong-pass"})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    r = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    r = await client.get("/api/v1/users/me")
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def _deactivate_user(user_id):
    # 复用 client fixture 注入的测试库会话
    gen = app.dependency_overrides[get_session]()
    session = await anext(gen)
    (await session.get(User, user_id)).is_active = False
    await session.commit()
    await gen.aclose()


async def test_unknown_user_same_error_as_wrong_password(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    wrong_pw = await client.post(
        "/api/v1/auth/login", json={"username": "kai", "password": "x" * 8}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "x" * 8}
    )
    # 走到哈希比对分支不能崩,且响应不暴露用户名是否存在
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["message"] == wrong_pw.json()["message"]


async def test_inactive_user_cannot_login_or_use_token(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    login = {"username": "kai", "password": "password123"}
    token = (await client.post("/api/v1/auth/login", json=login)).json()["access_token"]

    await _deactivate_user(2)  # 1 号是 admin fixture

    r = await client.post("/api/v1/auth/login", json=login)
    assert r.status_code == 401
    r = await client.get("/api/v1/users/me", headers=_bearer(token))
    assert r.status_code == 401


async def test_valid_signature_but_bad_subject_rejected(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    for sub in ("abc", "999"):  # 非数字 / 用户不存在
        r = await client.get("/api/v1/users/me", headers=_bearer(create_access_token(sub)))
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    no_sub = jwt.encode({}, get_settings().JWT_SECRET_KEY, algorithm=ALGORITHM)
    r = await client.get("/api/v1/users/me", headers=_bearer(no_sub))
    assert r.status_code == 401


async def test_expired_token_message(client):
    r = await client.get("/api/v1/users/me", headers=_bearer(create_access_token("1", -1)))
    assert r.status_code == 401 and r.json()["message"] == "凭证已过期"


# ---- refresh / logout ----


async def _login(client, creds=LOGIN):
    """登录并返回 (access_token, refresh cookie 值)。"""
    r = await client.post("/api/v1/auth/login", json=creds)
    assert "refresh_token" not in r.json()  # refresh 只在 HttpOnly cookie 里
    return r.json()["access_token"], client.cookies.get("refresh_token")


def _use(client, token):
    """让 client 之后只带指定的 refresh cookie(模拟另一台设备/旧 token)。"""
    client.cookies.clear()
    if token:
        client.cookies.set("refresh_token", token, path="/api/v1/auth")


async def _refresh(client, token):
    _use(client, token)
    return await client.post("/api/v1/auth/refresh")


def _jti(token):
    return jwt.decode(token, options={"verify_signature": False})["jti"]


async def test_login_sets_httponly_cookie_and_stores_jti(client, admin, redis):
    r = await client.post("/api/v1/auth/login", json=LOGIN)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/v1/auth" in cookie
    assert redis.ttl[f"refresh:{_jti(client.cookies.get('refresh_token'))}"] == 7 * 86400


async def test_refresh_gives_working_access_token(client, admin):
    _, refresh = await _login(client)
    r = await _refresh(client, refresh)
    assert r.status_code == 200
    r = await client.get("/api/v1/users/me", headers=_bearer(r.json()["access_token"]))
    assert r.status_code == 200 and r.json()["username"] == "root"


async def test_token_type_mixup_and_missing_cookie(client, admin):
    access, refresh = await _login(client)
    assert (await _refresh(client, access)).status_code == 401  # access 冒充 refresh
    assert (await _refresh(client, None)).status_code == 401  # 没带 cookie
    r = await client.get("/api/v1/users/me", headers=_bearer(refresh))  # refresh 冒充 access
    assert r.status_code == 401


async def test_rotation_invalidates_old_refresh(client, admin, redis):
    _, old = await _login(client)
    old_key = f"refresh:{_jti(old)}"
    assert old_key in redis.data
    assert (await _refresh(client, old)).status_code == 200
    assert old_key not in redis.data  # 轮换时旧 jti 被删除
    assert (await _refresh(client, old)).status_code == 401  # 旧的不能再用
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_logout_revokes_refresh_and_clears_cookie(client, admin):
    access, refresh = await _login(client)
    r = await client.post("/api/v1/auth/logout", headers=_bearer(access))
    assert r.status_code == 204 and "max-age=0" in r.headers["set-cookie"].lower()
    assert (await _refresh(client, refresh)).status_code == 401


async def test_logout_requires_access_token_and_own_refresh(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    root_access, _ = await _login(client)
    _, kai_refresh = await _login(client, KAI)
    assert (await client.post("/api/v1/auth/logout")).status_code == 401
    # root 不能用自己的 access 撤销 kai 的 refresh
    _use(client, kai_refresh)
    r = await client.post("/api/v1/auth/logout", headers=_bearer(root_access))
    assert r.status_code == 401
    assert (await _refresh(client, kai_refresh)).status_code == 200


async def test_logout_with_garbage_cookie_still_clears_it(client, admin):
    access, _ = await _login(client)
    _use(client, "not-a-jwt")
    r = await client.post("/api/v1/auth/logout", headers=_bearer(access))
    assert r.status_code == 204 and "max-age=0" in r.headers["set-cookie"].lower()


async def test_inactive_user_keeps_unconsumed_refresh(client, admin, redis):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    _, kai_refresh = await _login(client, KAI)
    await _deactivate_user(2)
    assert (await _refresh(client, kai_refresh)).status_code == 401
    assert f"refresh:{_jti(kai_refresh)}" in redis.data  # 校验失败不烧 token


async def test_refresh_rejected_for_inactive_user(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    _, kai_refresh = await _login(client, KAI)
    await _deactivate_user(2)
    assert (await _refresh(client, kai_refresh)).status_code == 401


async def test_single_login_user_kicks_old_session(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)  # 默认 allow_multi_login=False
    _, first = await _login(client, KAI)
    _, second = await _login(client, KAI)
    assert (await _refresh(client, first)).status_code == 401  # 被后登录的踢掉
    assert (await _refresh(client, second)).status_code == 200


async def test_multi_login_user_keeps_both_sessions(client, admin):
    _, first = await _login(client)  # admin 由 seed 置 allow_multi_login=True
    _, second = await _login(client)
    assert (await _refresh(client, first)).status_code == 200
    assert (await _refresh(client, second)).status_code == 200


async def test_logout_all_revokes_every_session(client, admin):
    access, first = await _login(client)
    _, second = await _login(client)
    _use(client, second)
    assert (
        await client.post("/api/v1/auth/logout-all", headers=_bearer(access))
    ).status_code == 204
    assert (await _refresh(client, first)).status_code == 401
    assert (await _refresh(client, second)).status_code == 401


async def test_redis_down_returns_503(client, admin):
    class Down:
        async def eval(self, *a):  # 限流 fail-open 放行后,才轮到登录自己的 Redis 调用报 503
            raise RedisConnectionError("down")

    app.dependency_overrides[get_redis] = lambda: Down()
    r = await client.post("/api/v1/auth/login", json=LOGIN)
    assert r.status_code == 503 and r.json()["code"] == "DATABASE_UNAVAILABLE"
