"""SPEC-014 审计日志验收测试(纯 ASGI 中间件方案)。

第一期范围:只记【成功】的写操作;中间件在响应发出后用独立会话写入;
审计失败只打 error 日志,不影响业务。
action = 接口所需的权限码;未走 require_permission 的接口(login/refresh 等)不记。
403 / 401 / 409 / 400 一律不记;GET 不记。
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import DataError

from app.main import app
from app.models import AuditLog
from tests.helpers import PAYLOAD
from tests.helpers import bearer as _bearer
from tests.helpers import make_user_with_perms as _make_user_with_perms

BASE = "/api/v1"
USERS = f"{BASE}/users"
AUDIT = f"{BASE}/audit-logs"


async def _logs(client, admin, **params):
    r = await client.get(AUDIT, params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


async def _total(client, admin):
    async with app.state.audit_session_factory() as session:
        return await session.scalar(select(func.count()).select_from(AuditLog))


async def _newest(client, admin):
    return (await _logs(client, admin))["items"][0]


# ---------- 记什么 ----------


async def test_create_user_is_audited(client, admin):
    r = await client.post(USERS, json=PAYLOAD, headers=admin)
    assert r.status_code == 201
    item = await _newest(client, admin)
    assert item["user_id"] == 1  # admin fixture 种下的第一个用户
    assert item["action"] == "admin:user:create"
    assert item["resource"] == "users"
    assert item["resource_id"] == r.json()["id"]  # 新建资源:id 取自响应体
    async with app.state.audit_session_factory() as s:  # trace_id 不在 API 返回里,直接查库
        assert await s.scalar(select(AuditLog.trace_id)) == r.headers["X-Trace-Id"]
    assert item["detail"]["method"] == "POST"
    # 默认全记,但标了 NO_AUDIT 的 password 不记
    assert item["detail"]["body"] == {
        "username": PAYLOAD["username"],
        "email": PAYLOAD["email"],
    }  # 记的是客户端实际发送的字段,没传的默认值不会出现
    assert item["detail"]["route"] == "/users"
    assert item["created_at"]


async def test_other_write_routes_are_audited(client, admin):
    role = (await client.post(f"{BASE}/roles", json={"name": "ops"}, headers=admin)).json()
    item = await _newest(client, admin)
    assert (item["action"], item["resource"], item["resource_id"]) == (
        "admin:role:create",
        "roles",
        role["id"],
    )
    assert item["detail"]["body"] == {"name": "ops"}

    uid = (await client.post(USERS, json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.put(f"{USERS}/{uid}/roles", json={"role_ids": [role["id"]]}, headers=admin)
    assert r.status_code == 204  # 204 没有响应体:id 必须来自路径参数
    item = await _newest(client, admin)
    assert (item["action"], item["resource"], item["resource_id"]) == (
        "admin:user:assign_role",
        "users",
        uid,
    )
    assert item["detail"]["route"] == "/users/{user_id}/roles"
    assert item["detail"]["path_params"] == {"user_id": str(uid)}
    assert item["detail"]["body"] == {"role_ids": [role["id"]]}

    r = await client.put(
        f"{BASE}/roles/{role['id']}/permissions",
        json={"permissions": ["admin:user:read"]},
        headers=admin,
    )
    assert r.status_code == 200
    item = await _newest(client, admin)
    assert (item["action"], item["resource"], item["resource_id"]) == (
        "admin:role:set_permissions",
        "roles",
        role["id"],
    )

    new = {"code": "admin:report:export", "name": "导出报表"}
    assert (await client.post(f"{BASE}/permissions", json=new, headers=admin)).status_code == 201
    item = await _newest(client, admin)
    assert (item["action"], item["resource"]) == ("admin:permission:create", "permissions")
    assert item["resource_id"] is None  # 权限以 code 为主键,响应里没有 id;已知局限
    assert item["detail"]["route"] == "/permissions"


async def test_detail_has_no_secrets(client, admin):
    await client.post(USERS, json=PAYLOAD, headers=admin)
    dumped = json.dumps(await _logs(client, admin))
    assert PAYLOAD["password"] not in dumped
    assert "password" not in dumped.lower()


async def test_audit_records_client_ip(client, admin):
    transport = ASGITransport(app=app, client=("203.0.113.7", 5000), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(USERS, json=PAYLOAD, headers=admin)
    assert r.status_code == 201
    assert (await _newest(client, admin))["ip"] == "203.0.113.7"


async def test_long_user_agent_does_not_lose_the_record(client, admin):
    # SQLite 不校验 VARCHAR 长度而 Postgres 会拒绝超长值;fail-open 又会吞掉失败,所以写入前必须截断
    headers = {**admin, "User-Agent": "x" * 1000}
    assert (await client.post(USERS, json=PAYLOAD, headers=headers)).status_code == 201
    async with app.state.audit_session_factory() as s:
        ua = await s.scalar(select(AuditLog.user_agent))
    assert ua and len(ua) <= 255


# ---------- 不记什么 ----------


async def test_failed_business_request_is_not_audited(client, admin):
    assert (await client.post(USERS, json=PAYLOAD, headers=admin)).status_code == 201
    before = await _total(client, admin)
    r = await client.post(USERS, json=PAYLOAD, headers=admin)  # 用户名重复
    assert r.status_code == 409
    assert await _total(client, admin) == before


async def test_invalid_body_is_not_audited(client, admin):
    before = await _total(client, admin)
    r = await client.post(USERS, json={"username": "ab"}, headers=admin)
    assert r.status_code == 400
    assert await _total(client, admin) == before


async def test_forbidden_and_unauthenticated_are_not_audited(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    before = await _total(client, admin)
    other = {**PAYLOAD, "username": "other", "email": "o@example.com"}
    assert (await client.post(USERS, json=other, headers=_bearer(uid))).status_code == 403
    assert (await client.post(USERS, json=other)).status_code == 401
    assert await _total(client, admin) == before


async def test_reads_are_not_audited(client, admin):
    before = await _total(client, admin)
    for url in (f"{USERS}/1", f"{USERS}/me", f"{BASE}/roles"):
        assert (await client.get(url, headers=admin)).status_code == 200
    # 同时证明 GET /audit-logs 自己不产生记录
    assert await _total(client, admin) == before


# ---------- 旁路:审计失败不影响业务 ----------


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("audit db down"),
        DataError(
            "INSERT INTO audit_logs ...", {"body": "audit-secret"}, ValueError("audit-secret")
        ),
    ],
)
async def test_audit_failure_does_not_break_business(client, admin, monkeypatch, caplog, failure):
    def broken_factory():
        raise failure

    monkeypatch.setattr(app.state, "audit_session_factory", broken_factory)

    # raise_app_exceptions=True:后台任务里的异常若没被审计代码自己吞掉,会直接抛进这个 await
    transport = ASGITransport(app=app, raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://test") as strict:
        r = await strict.post(USERS, json=PAYLOAD, headers=admin)
    assert r.status_code == 201
    assert (await client.get(f"{USERS}/{r.json()['id']}", headers=admin)).status_code == 200
    assert any("审计日志写入失败" in rec.getMessage() for rec in caplog.records)
    assert "audit-secret" not in caplog.text


# ---------- GET /audit-logs ----------


async def test_audit_logs_pagination(client, admin):
    ids = []
    for i in range(5):
        body = {**PAYLOAD, "username": f"user{i}", "email": f"u{i}@example.com"}
        r = await client.post(USERS, json=body, headers=admin)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    newest_first = ids[::-1]

    pages, params = [], {"limit": 2}
    while True:
        page = await _logs(client, admin, **params)
        pages.append(page)
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]
    assert [len(p["items"]) for p in pages] == [2, 2, 1]
    got = [[i["resource_id"] for i in p["items"]] for p in pages]
    assert got == [newest_first[:2], newest_first[2:4], newest_first[4:]]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "x" * 65},
        {"cursor": "bad!"},
    ],
)
async def test_audit_logs_rejects_bad_params(client, admin, params):
    r = await client.get(AUDIT, params=params, headers=admin)
    assert r.status_code == 400


async def test_audit_action_rejects_ilike(client, admin):
    # 必须是合法条件组,才能走到算子白名单;action 只允许 eq/in
    group = '{"conditions":[{"field":"action","op":"ilike","value":"admin%"}]}'
    r = await client.get(AUDIT, params={"filter": group}, headers=admin)
    assert r.status_code == 400 and r.json()["message"] == "筛选算子不支持"


async def test_audit_logs_requires_audit_read(client, admin):
    assert (await client.get(AUDIT)).status_code == 401
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    r = await client.get(AUDIT, headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["detail"] == "admin:audit:read"


async def test_audit_logs_allowed_with_audit_read(client, admin):
    # 角色创建会校验权限码存在,所以这条同时证明 seed 里加了 audit:read
    uid = await _make_user_with_perms(client, admin, ["admin:audit:read"])
    assert (await client.get(AUDIT, headers=_bearer(uid))).status_code == 200


# ---------- 黑名单标记 ----------


def test_audit_fields_exclude_marked_and_sensitive_fields():
    from types import SimpleNamespace

    from pydantic import BaseModel, Field

    from app.middlewares.audit_middleware import _audit_fields
    from app.schemas.base import NO_AUDIT

    class Body(BaseModel):
        name: str
        pin: str = Field(json_schema_extra=NO_AUDIT)  # 靠标记隐藏
        api_token: str  # 忘了标,靠名字兜底

    route = SimpleNamespace(body_field=SimpleNamespace(field_info=SimpleNamespace(annotation=Body)))
    assert _audit_fields(route) == {"name"}
    assert _audit_fields(SimpleNamespace()) == set()  # 没有请求体的路由


async def test_oversized_body_is_not_recorded(client, admin):
    body = {**PAYLOAD, "username": "big", "email": "b@example.com", "pad": "x" * 70_000}
    assert (await client.post(USERS, json=body, headers=admin)).status_code == 201
    assert "body" not in (await _newest(client, admin))["detail"]


async def test_undeclared_sensitive_key_is_not_recorded(client, admin):
    # schema 里没声明的字段会被 Pydantic 忽略,但原始请求体里有,不能被记下来
    body = {
        **PAYLOAD,
        "api_token": "tok-should-not-leak",
        "metadata": {"password": "tok-should-not-leak"},
        "items": [{"api_token": "tok-should-not-leak"}],
    }
    assert (await client.post(USERS, json=body, headers=admin)).status_code == 201
    assert "tok-should-not-leak" not in json.dumps(await _logs(client, admin))


async def test_nested_body_is_redacted(client, admin):
    from types import SimpleNamespace

    from pydantic import BaseModel

    from app.middlewares.audit_middleware import AuditMiddleware

    class Body(BaseModel):
        metadata: dict

    route = SimpleNamespace(
        path="/example", body_field=SimpleNamespace(field_info=SimpleNamespace(annotation=Body))
    )
    scope = {"app": app, "route": route, "path": "/example", "method": "POST", "headers": []}
    payload = {"metadata": {"name": "kept", "items": [{"token": "hidden", "count": 2}]}}
    await AuditMiddleware(app)._record(
        scope,
        {"audit_user_id": 1, "audit_action": "test:example:create"},
        "test",
        b"",
        json.dumps(payload).encode(),
    )
    item = await _newest(client, admin)
    assert item["detail"]["body"] == {"metadata": {"name": "kept", "items": [{"count": 2}]}}
