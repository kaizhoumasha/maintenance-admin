import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, func, select

from app.models import AuditLog, AuditLogArchive, Role, User
from tests.helpers import raw_cursor

PATHS = ("/api/v1/users", "/api/v1/audit-logs", "/api/v1/audit-logs/archive")


def condition(field, op, value=None):
    return {"field": field, "op": op, "value": value}


def group(*nodes, couple="and"):
    return {"couple": couple, "conditions": list(nodes)}


async def query(account, path, *nodes, **params):
    if nodes:
        params["filter"] = json.dumps(group(*nodes))
    return await account["client"].get(path, headers=account["headers"], params=params)


@pytest.mark.parametrize("path", PATHS)
async def test_pg_permissions_and_invalid_inputs(pg_client, path, caplog):
    client, tag = pg_client["client"], pg_client["tag"]
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=pg_client["denied"])).status_code == 403
    assert (await query(pg_client, path)).status_code == 200
    for params in (
        {"filer": tag},
        {"filter": ""},
        {"filter": "null"},
        {"filter": "[" * 900 + "]" * 900},
        {"filter": "汉" * 683},
    ):
        response = await query(pg_client, path, **params)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "INVALID_REQUEST"
        assert tag not in response.text
    for field in ("hashed_password", "detail", "not_a_column"):
        response = await query(pg_client, path, condition(field, "eq", tag))
        assert response.status_code == 400
        assert response.json()["message"] == "筛选字段不支持"
        assert tag not in response.text
    fields = ("role_id",) if path == PATHS[0] else ("user_id", "resource_id")
    for field in fields:
        for value in (True, 1.0, "1", 0, 2147483648):
            response = await query(pg_client, path, condition(field, "eq", value))
            assert response.status_code == 400, response.text
    if path != PATHS[0]:
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"cursor": raw_cursor({"id": 2147483648})},
            {"cursor": "*"},
        ):
            assert (await query(pg_client, path, **params)).status_code == 400
        assert (await query(pg_client, path, condition("action", "ilike", "%"))).status_code == 400
    assert tag not in "\n".join(
        r.getMessage() for r in caplog.records if r.name == "enterprise-backend"
    )
    assert (await query(pg_client, path)).status_code == 200


@pytest.mark.parametrize("path", PATHS)
async def test_pg_dates_and_string_validation(pg_client, path, caplog):
    bad_dates = [
        1700000000,
        "1700000000",
        "1700000000.5",
        "1700000000000",
        "20261017",
        "2026-10-17",
        "2026-10-17T12:00:00",
        "9999-12-31T23:59:59-01:00",
        "0001-01-01T00:00:00+01:00",
        "2026-10-17\x0012:00:00Z",
    ]
    for value in bad_dates:
        response = await query(pg_client, path, condition("created_at", "ge", value))
        assert response.status_code == 400, response.text
        assert str(value) not in response.text
    for value in bad_dates[-3:-1]:
        for endpoints in ([value, "2026-10-17T00:00:00Z"], ["2026-10-17T00:00:00Z", value]):
            response = await query(pg_client, path, condition("created_at", "between", endpoints))
            assert response.status_code == 400, response.text
            assert value not in response.text
    for value in ("0001-01-01T01:00:00+01:00", "9999-12-31T22:59:59-01:00"):
        assert (
            await query(pg_client, path, condition("created_at", "ge", value))
        ).status_code == 200
    field = "username" if path == PATHS[0] else "action"
    assert (
        await query(pg_client, path, condition(field, "eq", "private\x00value"))
    ).status_code == 400
    app_logs = "\n".join(r.getMessage() for r in caplog.records if r.name == "enterprise-backend")
    for value in bad_dates[1:]:
        assert value not in app_logs
    assert "private" not in app_logs


async def test_pg_user_fields_patterns_and_role_count(pg_client, pg_sessions):
    tag = pg_client["tag"]
    async with pg_sessions() as session:
        roles = [Role(name=f"{i}_{tag}", permissions=[]) for i in range(2)]
        pg_client["roles"].extend(roles)
        users = [
            User(
                username=name,
                email=f"{i}_{tag}@example.test",
                hashed_password="x",
                is_active=active,
                roles=roles if i == 0 else [],
                created_at=datetime(2026, 10, 17, hour, tzinfo=UTC),
            )
            for i, (name, active, hour) in enumerate(
                [(f"Alpha_{tag}", True, 12), (f"a_b%\\{tag}", False, 5)]
            )
        ]
        pg_client["users"].extend(users)
        session.add_all(users)
        await session.commit()
    scope = condition("email", "in", [u.email for u in users])
    cases = [
        (condition("username", "eq", users[0].username), [users[0].id]),
        (condition("username", "in", [u.username for u in users]), [u.id for u in reversed(users)]),
        (condition("email", "eq", users[1].email), [users[1].id]),
        (condition("email", "ilike", users[0].email.upper()), [users[0].id]),
        (condition("is_active", "eq", False), [users[1].id]),
        (condition("role_id", "eq", roles[0].id), [users[0].id]),
        (condition("role_id", "in", [r.id for r in roles]), [users[0].id]),
        (condition("role_id", "eq", 2147483647), []),
        (
            condition("created_at", "ge", "2026-10-17T13:00:00+08:00"),
            [u.id for u in reversed(users)],
        ),
        (condition("created_at", "le", "2026-10-17T13:00:00+08:00"), [users[1].id]),
        (
            condition("created_at", "between", ["2026-10-17T05:00:00Z", "2026-10-17T12:00:00Z"]),
            [u.id for u in reversed(users)],
        ),
        (condition("username", "ilike", f"ALPHA\\_{tag}"), [users[0].id]),
        (condition("username", "ilike", "ALPHA%"), [users[0].id]),
        (condition("username", "ilike", f"%{tag}%"), [u.id for u in reversed(users)]),
        (condition("username", "ilike", rf"a\_b\%\\{tag}"), [users[1].id]),
    ]
    for node, expected in cases:
        response = await query(pg_client, PATHS[0], scope, node, page=1, page_size=100)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [u["id"] for u in body["items"]] == expected
        assert body["total"] == len(expected)
        assert all("hashed_password" not in u for u in body["items"])
    for pattern in ("private\\", "x" * 65):
        assert (
            await query(pg_client, PATHS[0], condition("username", "ilike", pattern))
        ).status_code == 400
    for value in (0, 1, "true"):
        assert (
            await query(pg_client, PATHS[0], condition("is_active", "eq", value))
        ).status_code == 400
    response = await query(pg_client, PATHS[0], scope, page=2, page_size=1)
    assert response.json()["total"] == 2
    assert [u["id"] for u in response.json()["items"]] == [users[0].id]


@pytest.fixture
async def audit_rows(pg_client, pg_sessions):
    tag, uid = pg_client["tag"], pg_client["users"][0].id
    async with pg_sessions() as session:
        rows = [
            AuditLog(
                resource=tag,
                resource_id=i + 1,
                user_id=uid if i % 2 else None,
                action=f"hot_{tag}" if i < 4 else f"other_{tag}",
                trace_id=tag,
                created_at=datetime(2026, 10, 17, 5 if i % 2 else 12, tzinfo=UTC),
            )
            for i in range(5)
        ]
        session.add_all(rows)
        await session.flush()
        archives = [
            AuditLogArchive(
                id=r.id,
                resource=tag,
                resource_id=r.resource_id,
                user_id=r.user_id,
                action=f"cold_{tag}",
                trace_id=tag,
                created_at=r.created_at,
            )
            for r in rows
        ]
        session.add_all(archives)
        await session.commit()
    return rows, archives


@pytest.mark.parametrize("path,index,action", [(PATHS[1], 0, "hot"), (PATHS[2], 1, "cold")])
async def test_pg_audit_fields_cursor_sql_and_no_writes(
    pg_client, pg_sessions, audit_rows, path, index, action
):
    tag, uid = pg_client["tag"], pg_client["users"][0].id
    rows = audit_rows[index]
    scope = condition("resource", "eq", tag)
    expected = [r.id for r in reversed(rows) if r.action == f"{action}_{tag}"]
    sql = []
    engine = pg_sessions.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        if f"FROM {AuditLog.__table__.fullname}" in statement:
            sql.append(statement.upper())

    async with pg_sessions() as session:
        before = [
            await session.scalar(select(func.count()).select_from(m))
            for m in (AuditLog, AuditLogArchive)
        ]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        ids, cursor = [], None
        while True:
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            response = await query(
                pg_client, path, scope, condition("action", "in", [f"{action}_{tag}"]), **params
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == {"items", "next_cursor"}
            ids.extend(r["id"] for r in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert ids == expected
        assert sql and all("COUNT(" not in s and "OFFSET" not in s for s in sql)
        assert all(("AUDIT_LOGS_ARCHIVE" in s) == (index == 1) for s in sql)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    cases = [
        (condition("user_id", "eq", uid), [rows[3].id, rows[1].id]),
        (condition("user_id", "in", [uid]), [rows[3].id, rows[1].id]),
        (condition("user_id", "is_null"), [rows[4].id, rows[2].id, rows[0].id]),
        (condition("user_id", "not_null"), [rows[3].id, rows[1].id]),
        (group(condition("user_id", "eq", uid), couple="not"), []),
        (
            group(
                group(condition("user_id", "eq", uid), couple="not"),
                condition("user_id", "is_null"),
                couple="or",
            ),
            [rows[4].id, rows[2].id, rows[0].id],
        ),
        (condition("resource_id", "in", [1, 3]), [rows[2].id, rows[0].id]),
        (condition("resource_id", "eq", 2), [rows[1].id]),
        (condition("resource", "in", [tag]), [r.id for r in reversed(rows)]),
        (condition("action", "eq", f"{'cold' if index == 0 else 'hot'}_{tag}"), []),
        (condition("created_at", "le", "2026-10-17T13:00:00+08:00"), [rows[3].id, rows[1].id]),
    ]
    for node, ids in cases:
        response = await query(pg_client, path, scope, node)
        assert response.status_code == 200, response.text
        assert [r["id"] for r in response.json()["items"]] == ids
    assert (await query(pg_client, path, scope, limit=100)).json()["next_cursor"] is None
    assert (await query(pg_client, path, scope, condition("resource_id", "eq", 99))).json() == {
        "items": [],
        "next_cursor": None,
    }
    assert (await query(pg_client, path, condition("detail", "eq", "private"))).status_code == 400
    async with pg_sessions() as session:
        after = [
            await session.scalar(select(func.count()).select_from(m))
            for m in (AuditLog, AuditLogArchive)
        ]
    assert after == before


async def test_pg_insert_and_archive_between_pages(pg_client, pg_sessions, audit_rows):
    rows, _ = audit_rows
    tag = pg_client["tag"]
    scope = condition("resource", "eq", tag)
    first = (await query(pg_client, PATHS[1], scope, limit=1)).json()
    assert first["items"][0]["id"] == rows[-1].id
    async with pg_sessions() as session:
        newest = AuditLog(resource=tag, action=tag, trace_id=tag)
        session.add(newest)
        # 对应归档副本已存在于测试数据集,只移走尚未返回的热表记录。
        await session.execute(delete(AuditLog).where(AuditLog.id == rows[-2].id))
        await session.commit()
    rest = (await query(pg_client, PATHS[1], scope, limit=100, cursor=first["next_cursor"])).json()
    assert [r["id"] for r in rest["items"]] == [r.id for r in reversed(rows[:-2])]
    assert rest["next_cursor"] is None
    assert (await query(pg_client, PATHS[1], scope, limit=1)).json()["items"][0]["id"] == newest.id
