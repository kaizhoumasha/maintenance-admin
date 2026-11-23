"""公开列表的 start/end 分页契约。"""

import json

import pytest
from pydantic import ValidationError

from app.schemas.base import PageParams
from tests.helpers import PAYLOAD
from tests.helpers import bearer as _bearer
from tests.helpers import make_user_with_perms as _make_user_with_perms

BASE = "/api/v1"
USERS = f"{BASE}/users"
AUDIT = f"{BASE}/audit-logs"


async def test_users_and_audit_logs_page_shapes(client, admin):
    await client.post(USERS, json=PAYLOAD, headers=admin)
    users = (await client.get(USERS, headers=admin)).json()
    audit = (await client.get(AUDIT, headers=admin)).json()
    assert set(users) == {"items", "total", "start", "end"}
    assert set(audit) == {"items", "total", "start", "end"}
    assert (users["start"], users["end"]) == (1, 20)


async def test_users_pagination(client, admin):
    for i in range(4):
        body = {**PAYLOAD, "username": f"user{i}", "email": f"u{i}@example.com"}
        assert (await client.post(USERS, json=body, headers=admin)).status_code == 201

    pages = [
        (await client.get(USERS, params={"start": s, "end": s + 1}, headers=admin)).json()
        for s in (1, 3, 5)
    ]
    assert all(p["total"] == 5 for p in pages)  # 4 个新用户 + admin
    ids = [[u["id"] for u in p["items"]] for p in pages]
    assert [len(x) for x in ids] == [2, 2, 1]
    flat = [i for x in ids for i in x]
    assert flat == sorted(flat, reverse=True)  # id 倒序且不重不漏
    assert "hashed_password" not in pages[0]["items"][0]


@pytest.mark.parametrize(
    "params",
    [
        {"start": 0},
        {"end": 0},
        {"start": 1, "end": 101},
        {"start": 1},
        {"end": 20},
        {"start": 2, "end": 1},
    ],
)
async def test_users_rejects_bad_paging(client, admin, params):
    assert (await client.get(USERS, params=params, headers=admin)).status_code == 400


async def test_users_list_requires_list_permission(client, admin):
    assert (await client.get(USERS)).status_code == 401
    uid = await _make_user_with_perms(
        client, admin, ["admin:user:read"]
    )  # 能读单个用户不等于能列表
    r = await client.get(USERS, headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["detail"] == "admin:user:list"


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"start": 100000, "end": 100000}, 200),
        ({"start": 100001, "end": 100001}, 200),
        ({"start": 100002, "end": 100002}, 400),
        ({"start": 9223372036854775808, "end": 9223372036854775808}, 400),
        ({"start": 9223372036854775809, "end": 9223372036854775809}, 400),
        ({"start": 10**30, "end": 10**30}, 400),
    ],
)
async def test_pagination_offset_bounds(client, admin, params, status):
    response = await client.get(USERS, params=params, headers=admin)
    assert response.status_code == status
    if status == 400:
        assert response.json()["code"] == "INVALID_REQUEST"
    else:
        assert response.json()["items"] == []


def test_page_params_preserves_int64_overflow_validation():
    with pytest.raises(ValidationError, match="分页 OFFSET 超出数据库范围"):
        PageParams(start=9223372036854775809, end=9223372036854775809)


@pytest.mark.parametrize("path", [USERS, AUDIT, f"{AUDIT}/archive"])
async def test_filter_query_combines_with_paging_and_rejects_unknown(client, admin, path):
    field = "username" if path == USERS else "action"
    raw = json.dumps({"conditions": [{"field": field, "op": "eq", "value": "no-match"}]})
    paging = {"start": 1, "end": 1}
    result = await client.get(path, headers=admin, params={**paging, "filter": raw})
    assert result.status_code == 200, result.text
    assert result.json()["items"] == []
    for params in (
        {"filter": ""},
        {"filter": "null"},
        {"filter": "[" * 900 + "]" * 900},
        {"filter": "汉" * 683},
    ):
        result = await client.get(path, headers=admin, params=params)
        assert result.status_code == 400, result.text
        assert result.json()["code"] == "INVALID_REQUEST"


async def test_query_parameters_are_flat_in_openapi(client):
    from app.main import app

    schema = app.openapi()
    for path in (USERS, AUDIT, f"{AUDIT}/archive"):
        operation = schema["paths"][path]["get"]
        names = {p["name"] for p in operation["parameters"]}
        assert names == ({"start", "end", "filter"})
