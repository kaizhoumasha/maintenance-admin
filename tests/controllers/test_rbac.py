import pytest

from tests.helpers import PAYLOAD
from tests.helpers import bearer as _bearer
from tests.helpers import make_user_with_perms as _make_user_with_perms


async def test_user_without_role_is_forbidden(client, admin):
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.get(f"/api/v1/users/{uid}", headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    # /me 只要求登录,不要求权限
    assert (await client.get("/api/v1/users/me", headers=_bearer(uid))).status_code == 200


async def test_permission_is_per_code(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    assert (await client.get("/api/v1/users/1", headers=_bearer(uid))).status_code == 200
    r = await client.post(
        "/api/v1/users",
        json={**PAYLOAD, "username": "x" * 3, "email": "x@e.com"},
        headers=_bearer(uid),
    )
    assert r.status_code == 403 and r.json()["detail"] == "admin:user:create"


async def test_revoking_permission_takes_effect_immediately(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    h = _bearer(uid)
    assert (await client.get("/api/v1/users/1", headers=h)).status_code == 200
    r = await client.put("/api/v1/roles/2/permissions", json={"permissions": []}, headers=admin)
    assert r.status_code == 200 and r.json()["permissions"] == []
    assert (await client.get("/api/v1/users/1", headers=h)).status_code == 403


async def test_role_admin_endpoints_need_permission(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    h = _bearer(uid)
    assert (await client.get("/api/v1/roles", headers=h)).status_code == 403
    assert (await client.post("/api/v1/roles", json={"name": "y"}, headers=h)).status_code == 403
    assert (
        await client.put("/api/v1/users/1/roles", json={"role_ids": []}, headers=h)
    ).status_code == 403


async def test_role_validation_and_conflicts(client, admin):
    r = await client.post("/api/v1/roles", json={"name": "admin"}, headers=admin)
    assert r.status_code == 409
    r = await client.post(
        "/api/v1/roles", json={"name": "z", "permissions": ["nope"]}, headers=admin
    )
    assert r.status_code == 400
    r = await client.put("/api/v1/roles/99/permissions", json={"permissions": []}, headers=admin)
    assert r.status_code == 404
    r = await client.put("/api/v1/users/1/roles", json={"role_ids": [99]}, headers=admin)
    assert r.status_code == 400 and r.json()["detail"] == "99"
    r = await client.put("/api/v1/users/99/roles", json={"role_ids": []}, headers=admin)
    assert r.status_code == 404
    r = await client.get("/api/v1/roles", headers=admin)
    assert [x["name"] for x in r.json()] == ["admin"]


async def test_unauthenticated_gets_401_not_403(client):
    r = await client.get("/api/v1/roles")
    assert r.status_code == 401


async def test_permissions_are_data(client, admin):
    r = await client.get("/api/v1/permissions", headers=admin)
    assert "admin:user:read" in [x["code"] for x in r.json()]
    new = {"code": "admin:report:export", "name": "导出报表"}
    assert (await client.post("/api/v1/permissions", json=new, headers=admin)).status_code == 201
    assert (await client.post("/api/v1/permissions", json=new, headers=admin)).status_code == 409
    # 新建的权限可立即授予角色,并被鉴权识别
    r = await client.post(
        "/api/v1/roles", json={"name": "q", "permissions": ["admin:report:export"]}, headers=admin
    )
    assert r.status_code == 201 and r.json()["permissions"] == [new]


@pytest.mark.parametrize("value", [0, -1, 2147483648, 2**100])
async def test_invalid_ids_are_rejected_before_database_access(client, admin, value):
    requests = [
        await client.get(f"/api/v1/users/{value}", headers=admin),
        await client.put(f"/api/v1/users/{value}/roles", json={"role_ids": []}, headers=admin),
        await client.put(
            f"/api/v1/roles/{value}/permissions", json={"permissions": []}, headers=admin
        ),
        await client.put("/api/v1/users/1/roles", json={"role_ids": [value]}, headers=admin),
    ]
    for response in requests:
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REQUEST"


async def test_largest_valid_id_reaches_not_found_handling(client, admin):
    assert (await client.get("/api/v1/users/2147483647", headers=admin)).status_code == 404


async def test_me_returns_permission_codes(client, admin):
    r = await client.get("/api/v1/users/me", headers=admin)
    codes = r.json()["permissions"]
    assert "admin:user:create" in codes and codes == sorted(codes)

    uid = await _make_user_with_perms(client, admin, ["admin:user:read", "admin:role:read"])
    r = await client.get("/api/v1/users/me", headers=_bearer(uid))
    assert r.json()["permissions"] == ["admin:role:read", "admin:user:read"]


async def test_me_permissions_empty_without_role_and_deduplicated(client, admin):
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    assert (await client.get("/api/v1/users/me", headers=_bearer(uid))).json()["permissions"] == []

    # 两个角色都有同一权限,只返回一次
    roles = [
        (
            await client.post(
                "/api/v1/roles", json={"name": n, "permissions": ["admin:user:read"]}, headers=admin
            )
        ).json()["id"]
        for n in ("a", "b")
    ]
    await client.put(f"/api/v1/users/{uid}/roles", json={"role_ids": roles}, headers=admin)
    r = await client.get("/api/v1/users/me", headers=_bearer(uid))
    assert r.json()["permissions"] == ["admin:user:read"]


# ---- 角色改名 / 删除(SPEC-017) ----


async def _role(client, admin, name, perms=()):
    r = await client.post(
        "/api/v1/roles", json={"name": name, "permissions": list(perms)}, headers=admin
    )
    return r.json()["id"]


async def test_rename_role(client, admin):
    rid = await _role(client, admin, "ops", ["admin:user:read"])
    r = await client.put(f"/api/v1/roles/{rid}", json={"name": "ops2"}, headers=admin)
    assert r.status_code == 200
    assert r.json()["name"] == "ops2" and r.json()["permissions"][0]["code"] == "admin:user:read"
    # 改成自己当前的名字不算冲突
    assert (
        await client.put(f"/api/v1/roles/{rid}", json={"name": "ops2"}, headers=admin)
    ).status_code == 200


async def test_rename_role_errors(client, admin):
    rid = await _role(client, admin, "ops")
    r = await client.put(f"/api/v1/roles/{rid}", json={"name": "admin"}, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert (
        await client.put("/api/v1/roles/999", json={"name": "x"}, headers=admin)
    ).status_code == 404
    assert (
        await client.put(f"/api/v1/roles/{rid}", json={"name": ""}, headers=admin)
    ).status_code == 400


async def test_delete_role(client, admin):
    rid = await _role(client, admin, "ops", ["admin:user:read"])
    assert (await client.delete(f"/api/v1/roles/{rid}", headers=admin)).status_code == 204
    names = [x["name"] for x in (await client.get("/api/v1/roles", headers=admin)).json()]
    assert names == ["admin"]
    assert (await client.delete(f"/api/v1/roles/{rid}", headers=admin)).status_code == 404


async def test_delete_role_in_use_is_rejected(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:user:read"])
    r = await client.delete("/api/v1/roles/2", headers=admin)
    assert r.status_code == 409 and "关联用户" in r.json()["message"]
    # 解除绑定后可以删除
    await client.put(f"/api/v1/users/{uid}/roles", json={"role_ids": []}, headers=admin)
    assert (await client.delete("/api/v1/roles/2", headers=admin)).status_code == 204


async def test_role_update_delete_need_permission(client, admin):
    uid = await _make_user_with_perms(client, admin, ["admin:role:read"])
    h = _bearer(uid)
    r = await client.put("/api/v1/roles/1", json={"name": "x"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == "admin:role:update"
    r = await client.delete("/api/v1/roles/1", headers=h)
    assert r.status_code == 403 and r.json()["detail"] == "admin:role:delete"
