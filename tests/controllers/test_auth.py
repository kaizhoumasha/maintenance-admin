import jwt

from app.core import get_settings
from app.core.database import get_session
from app.core.security import ALGORITHM, create_access_token
from app.main import app
from app.models import User

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


async def test_login_then_me(client):
    await client.post("/api/v1/users", json=PAYLOAD)
    r = await client.post("/api/v1/auth/login", json={"username": "kai", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    r = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "kai"


async def test_bad_credentials_and_token(client):
    await client.post("/api/v1/users", json=PAYLOAD)
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


async def test_unknown_user_same_error_as_wrong_password(client):
    await client.post("/api/v1/users", json=PAYLOAD)
    wrong_pw = await client.post(
        "/api/v1/auth/login", json={"username": "kai", "password": "x" * 8}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"username": "nobody", "password": "x" * 8}
    )
    # 走到哈希比对分支不能崩,且响应不暴露用户名是否存在
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json()["message"] == wrong_pw.json()["message"]


async def test_inactive_user_cannot_login_or_use_token(client):
    await client.post("/api/v1/users", json=PAYLOAD)
    login = {"username": "kai", "password": "password123"}
    token = (await client.post("/api/v1/auth/login", json=login)).json()["access_token"]

    await _deactivate_user(1)

    r = await client.post("/api/v1/auth/login", json=login)
    assert r.status_code == 401
    r = await client.get("/api/v1/users/me", headers=_bearer(token))
    assert r.status_code == 401


async def test_valid_signature_but_bad_subject_rejected(client):
    await client.post("/api/v1/users", json=PAYLOAD)
    for sub in ("abc", "999"):  # 非数字 / 用户不存在
        r = await client.get("/api/v1/users/me", headers=_bearer(create_access_token(sub)))
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHORIZED"
    no_sub = jwt.encode({}, get_settings().JWT_SECRET_KEY, algorithm=ALGORITHM)
    r = await client.get("/api/v1/users/me", headers=_bearer(no_sub))
    assert r.status_code == 401


async def test_expired_token_message(client):
    r = await client.get("/api/v1/users/me", headers=_bearer(create_access_token("1", -1)))
    assert r.status_code == 401 and r.json()["message"] == "凭证已过期"
