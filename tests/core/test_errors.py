import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, SQLAlchemyError

from app.core import HTTP_STATUS, ErrorCode
from app.core.security import create_access_token
from app.services import user_service


def _assert_error(r, status: int, code: ErrorCode) -> dict:
    assert r.status_code == status
    body = r.json()
    assert set(body) == {"trace_id", "code", "message", "detail"}
    assert body["code"] == code
    assert body["trace_id"] != "-"
    assert r.headers["X-Trace-Id"] == body["trace_id"]
    return body


async def test_app_error(client, admin):
    _assert_error(await client.get("/api/v1/users/999", headers=admin), 404, ErrorCode.NOT_FOUND)


async def test_forbidden(client, admin):
    payload = {"username": "nobody", "email": "n@example.com", "password": "password123"}
    uid = (await client.post("/api/v1/users", json=payload, headers=admin)).json()["id"]
    token = create_access_token(str(uid))  # 已登录但没有任何角色
    r = await client.get("/api/v1/users/1", headers={"Authorization": f"Bearer {token}"})
    body = _assert_error(r, 403, ErrorCode.FORBIDDEN)
    assert body["detail"] == "admin:user:read"


async def test_validation_error(client, admin):
    r = await client.post("/api/v1/users", json={"username": "x"}, headers=admin)
    body = _assert_error(r, 400, ErrorCode.INVALID_REQUEST)
    assert "username" in body["detail"]


async def test_unknown_route(client):
    body = _assert_error(await client.get("/nope"), 404, ErrorCode.NOT_FOUND)
    assert body["message"] == "资源不存在"


async def test_method_not_allowed(client):
    body = _assert_error(await client.post("/health"), 405, ErrorCode.METHOD_NOT_ALLOWED)
    assert body["message"] == "请求方法不允许"


async def test_unhandled_error_hides_internals(client, admin, monkeypatch):
    async def boom(*_):
        raise RuntimeError("secret-password=abc")

    monkeypatch.setattr(user_service, "get_user", boom)
    r = await client.get("/api/v1/users/1", headers=admin)
    body = _assert_error(r, 500, ErrorCode.INTERNAL_ERROR)
    assert body["detail"] is None and "secret" not in r.text


async def test_too_many_requests_has_retry_after(client):
    body = {"username": "nobody", "password": "whatever123"}
    for _ in range(5):
        await client.post("/api/v1/auth/login", json=body)
    r = await client.post("/api/v1/auth/login", json=body)
    _assert_error(r, 429, ErrorCode.TOO_MANY_REQUESTS)
    assert r.headers["Retry-After"].isdigit()


def test_http_status_unique():
    # handlers.py 按状态码反查错误码，状态码重复会让结果不确定
    assert len(set(HTTP_STATUS.values())) == len(HTTP_STATUS)


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (
            IntegrityError("INSERT INTO user ...", {"email": "a@b.c"}, Exception("dup")),
            409,
            ErrorCode.CONFLICT,
        ),
        (
            OperationalError("SELECT 1", {"email": "a@b.c"}, Exception("conn refused")),
            503,
            ErrorCode.DATABASE_UNAVAILABLE,
        ),
        (
            ConnectionRefusedError("Connect call failed 127.0.0.1:25432"),
            503,
            ErrorCode.DATABASE_UNAVAILABLE,
        ),
        (SQLAlchemyError("boom password=abc"), 500, ErrorCode.INTERNAL_ERROR),
        (
            DataError(
                "INSERT INTO user ...",
                {"email": "a@b.c", "hash": "password=abc"},
                ValueError("invalid value: a@b.c"),
            ),
            500,
            ErrorCode.INTERNAL_ERROR,
        ),
    ],
)
async def test_db_errors(client, admin, monkeypatch, caplog, exc, status, code):
    async def boom(*_):
        raise exc

    monkeypatch.setattr(user_service, "get_user", boom)
    r = await client.get("/api/v1/users/1", headers=admin)
    body = _assert_error(r, status, code)
    assert body["detail"] == type(exc).__name__
    assert "a@b.c" not in r.text and "password=abc" not in r.text  # 参数和内部信息不返回
    assert any("database" in rec.getMessage() for rec in caplog.records)  # 定位信息进了日志
    assert "a@b.c" not in caplog.text  # SQL 参数不进日志
    assert "password=abc" not in caplog.text
    if not isinstance(exc, IntegrityError):
        assert "Traceback" in caplog.text


def test_error_responses_builds_openapi_responses():
    from app.core import ErrorResponse, error_responses

    assert error_responses(400, 409) == {
        400: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    }
