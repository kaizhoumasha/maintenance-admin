from app.core.security import create_access_token

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


def _bearer(user_id):
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _make_user_with_perms(client, admin, perms):
    """建一个只拥有 perms 的用户(经 API 走一遍 角色→权限→用户),返回其 id。"""
    role = (
        await client.post("/api/v1/roles", json={"name": "r", "permissions": perms}, headers=admin)
    ).json()
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.put(
        f"/api/v1/users/{uid}/roles", json={"role_ids": [role["id"]]}, headers=admin
    )
    assert r.status_code == 204
    return uid


async def test_user_without_role_is_forbidden(client, admin):
    uid = (await client.post("/api/v1/users", json=PAYLOAD, headers=admin)).json()["id"]
    r = await client.get(f"/api/v1/users/{uid}", headers=_bearer(uid))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    # /me 只要求登录,不要求权限
    assert (await client.get("/api/v1/users/me", headers=_bearer(uid))).status_code == 200


async def test_permission_is_per_code(client, admin):
    uid = await _make_user_with_perms(client, admin, ["user:read"])
    assert (await client.get("/api/v1/users/1", headers=_bearer(uid))).status_code == 200
    r = await client.post(
        "/api/v1/users",
        json={**PAYLOAD, "username": "x" * 3, "email": "x@e.com"},
        headers=_bearer(uid),
    )
    assert r.status_code == 403 and r.json()["detail"] == "user:create"


async def test_revoking_permission_takes_effect_immediately(client, admin):
    uid = await _make_user_with_perms(client, admin, ["user:read"])
    h = _bearer(uid)
    assert (await client.get("/api/v1/users/1", headers=h)).status_code == 200
    r = await client.put("/api/v1/roles/2/permissions", json={"permissions": []}, headers=admin)
    assert r.status_code == 200 and r.json()["permissions"] == []
    assert (await client.get("/api/v1/users/1", headers=h)).status_code == 403


async def test_role_admin_endpoints_need_permission(client, admin):
    uid = await _make_user_with_perms(client, admin, ["user:read"])
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
    assert "user:read" in [x["code"] for x in r.json()]
    new = {"code": "report:export", "name": "导出报表"}
    assert (await client.post("/api/v1/permissions", json=new, headers=admin)).status_code == 201
    assert (await client.post("/api/v1/permissions", json=new, headers=admin)).status_code == 409
    # 新建的权限可立即授予角色,并被鉴权识别
    r = await client.post(
        "/api/v1/roles", json={"name": "q", "permissions": ["report:export"]}, headers=admin
    )
    assert r.status_code == 201 and r.json()["permissions"] == [new]
