"""统一分页契约:/users 与 /audit-logs 响应结构一致。"""

import pytest

from tests.controllers.test_rbac import _bearer, _make_user_with_perms
from tests.controllers.test_users import PAYLOAD

BASE = "/api/v1"
USERS = f"{BASE}/users"
AUDIT = f"{BASE}/audit-logs"


async def test_users_and_audit_logs_share_page_shape(client, admin):
    await client.post(USERS, json=PAYLOAD, headers=admin)
    users = (await client.get(USERS, headers=admin)).json()
    audit = (await client.get(AUDIT, headers=admin)).json()
    assert set(users) == set(audit) == {"items", "total", "page", "page_size"}
    assert (users["page"], users["page_size"]) == (audit["page"], audit["page_size"]) == (1, 20)


async def test_users_pagination(client, admin):
    for i in range(4):
        body = {**PAYLOAD, "username": f"user{i}", "email": f"u{i}@example.com"}
        assert (await client.post(USERS, json=body, headers=admin)).status_code == 201

    pages = [
        (await client.get(USERS, params={"page": p, "page_size": 2}, headers=admin)).json()
        for p in (1, 2, 3)
    ]
    assert all(p["total"] == 5 for p in pages)  # 4 个新用户 + admin
    ids = [[u["id"] for u in p["items"]] for p in pages]
    assert [len(x) for x in ids] == [2, 2, 1]
    flat = [i for x in ids for i in x]
    assert flat == sorted(flat, reverse=True)  # id 倒序且不重不漏
    assert "hashed_password" not in pages[0]["items"][0]


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
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
        ({"page": 9223372036854775808, "page_size": 1}, 200),
        ({"page": 9223372036854775809, "page_size": 1}, 400),
        ({"page": 92233720368547759, "page_size": 100}, 200),
        ({"page": 92233720368547760, "page_size": 100}, 400),
        ({"page": 10**30, "page_size": 20}, 400),
    ],
)
async def test_pagination_offset_bounds(client, admin, params, status):
    for path in (USERS, AUDIT, f"{AUDIT}/archive"):
        response = await client.get(path, params=params, headers=admin)
        assert response.status_code == status
        if status == 400:
            assert response.json()["code"] == "INVALID_REQUEST"
        else:
            assert response.json()["items"] == []
