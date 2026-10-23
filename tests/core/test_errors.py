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


def test_openapi_error_responses_follow_route_rules():
    """规则而不是快照:只看 OpenAPI 输出,新增路由自动受约束。"""
    from app.core.deps import registered_codes
    from app.main import app

    schema = app.openapi()
    operations = [
        (path, op)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if method in ("get", "post", "put", "patch", "delete")
    ]
    assert len(operations) > 10
    for path, op in operations:
        codes = set(op["responses"])
        if path == "/api/v1/wms/events":
            assert "422" in codes and "429" not in codes and "200" not in codes
            assert "400" in codes and "413" in codes and "503" in codes
            continue
        if path == "/api/v1/callback/event":
            assert "429" not in codes and "422" not in codes
            assert "400" in codes and "413" in codes and "503" in codes
            continue
        assert "429" in codes, path  # 全局限流
        assert "422" not in codes, path  # 校验失败实际返回 400
        if op.get("parameters") or "requestBody" in op:
            assert "400" in codes, path
        if op.get("security"):
            assert "401" in codes, path
        for code in codes:
            if code[0] in "45":
                ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorResponse"), (path, code)
    # 一个权限码对应一个路由(导入时已强制),所以带 403 的操作数必须等于权限码数
    assert sum("403" in op["responses"] for _, op in operations) == len(registered_codes)
    for method, path, code in (
        ("post", "/api/v1/auth/login", "401"),
        ("get", "/api/v1/users/{user_id}", "404"),
        ("post", "/api/v1/users", "409"),
        ("get", "/ready", "503"),
    ):
        assert code in schema["paths"][path][method]["responses"], (path, code)
    assert "HTTPValidationError" not in schema["components"]["schemas"]
