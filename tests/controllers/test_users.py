import threading

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import user_service

PAYLOAD = {"username": "kai", "email": "k@example.com", "password": "password123"}


async def test_create_and_get_user(client, admin):
    r = await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert r.status_code == 201
    body = r.json()
    assert body["username"] == "kai" and "password" not in body and "hashed_password" not in body

    r = await client.get(f"/api/v1/users/{body['id']}", headers=admin)
    assert r.status_code == 200 and r.json()["email"] == "k@example.com"


async def test_duplicate_username_conflicts(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post(
        "/api/v1/users", json={**PAYLOAD, "email": "other@example.com"}, headers=admin
    )
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


async def test_get_missing_user_404(client, admin):
    r = await client.get("/api/v1/users/999", headers=admin)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


async def test_duplicate_email_conflicts(client, admin):
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    r = await client.post("/api/v1/users", json={**PAYLOAD, "username": "other"}, headers=admin)
    assert r.status_code == 409 and r.json()["message"] == "邮箱已被注册"


async def test_concurrent_duplicate_hits_unique_constraint(client, admin, monkeypatch, caplog):
    # 模拟并发:预检查都没查到,commit 时由真实的唯一约束报 IntegrityError,应被 handler 转成 409
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)

    async def no_hit(*_a, **_k):
        return None

    monkeypatch.setattr(AsyncSession, "scalar", no_hit)
    r = await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    rec = next(x for x in caplog.records if "integrity" in x.getMessage())
    assert rec.src.startswith("services/user_service.py:")


async def test_password_hashing_runs_off_event_loop(client, admin, monkeypatch):
    threads = []
    real = user_service.hash_password

    def spy(password):
        threads.append(threading.current_thread())
        return real(password)

    monkeypatch.setattr(user_service, "hash_password", spy)
    await client.post("/api/v1/users", json=PAYLOAD, headers=admin)
    assert threads and threads[0] is not threading.main_thread()


# ---- 用户管理(SPEC-017):修改 / 启停 / 改密 / 重置密码 / 保留管理员 ----

LOGIN = "/api/v1/auth/login"


async def _new_user(client, admin, username="kai", perms=None, role_ids=None):
    """建用户;perms 给定则建同名角色并分配,role_ids 给定则直接分配。返回 id。"""
    body = {**PAYLOAD, "username": username, "email": f"{username}@example.com"}
    uid = (await client.post("/api/v1/users", json=body, headers=admin)).json()["id"]
    if perms is not None:
        role = await client.post(
            "/api/v1/roles", json={"name": f"r-{username}", "permissions": perms}, headers=admin
        )
        role_ids = [role.json()["id"]]
    if role_ids:
        r = await client.put(
            f"/api/v1/users/{uid}/roles", json={"role_ids": role_ids}, headers=admin
        )
        assert r.status_code == 204
    return uid


async def _login(client, username="kai", password="password123"):
    r = await client.post(LOGIN, json={"username": username, "password": password})
    return r, {"Authorization": f"Bearer {r.json().get('access_token')}"}


async def test_update_user_fields(client, admin):
    uid = await _new_user(client, admin)
    r = await client.put(
        f"/api/v1/users/{uid}",
        json={"email": "new@example.com", "allow_multi_login": True},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["email"] == "new@example.com" and r.json()["allow_multi_login"] is True
    assert r.json()["is_active"] is True  # 未传的字段不变


async def test_update_user_errors(client, admin):
    uid = await _new_user(client, admin)
    r = await client.put(f"/api/v1/users/{uid}", json={"email": "root@example.com"}, headers=admin)
    assert r.status_code == 409 and r.json()["message"] == "邮箱已被注册"
    r = await client.put(f"/api/v1/users/{uid}", json={"email": "bad"}, headers=admin)
    assert r.status_code == 400
    r = await client.put("/api/v1/users/999", json={"is_active": True}, headers=admin)
    assert r.status_code == 404


async def test_disable_user_revokes_sessions_and_access(client, admin, redis):
    uid = await _new_user(client, admin)
    r, kai = await _login(client)
    assert f"user_refresh:{uid}" in redis.data
    assert (await client.get("/api/v1/users/me", headers=kai)).status_code == 200

    r = await client.put(f"/api/v1/users/{uid}", json={"is_active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert f"user_refresh:{uid}" not in redis.data
    assert (await client.get("/api/v1/users/me", headers=kai)).status_code == 401  # 立即失效
    assert (await _login(client))[0].status_code == 401


async def test_cannot_disable_self(client, admin):
    r = await client.put("/api/v1/users/1", json={"is_active": False}, headers=admin)
    assert r.status_code == 400 and r.json()["message"] == "不能停用自己"


async def test_last_admin_is_protected(client, admin):
    # root 是唯一拥有 admin:role:set_permissions 的活跃用户
    ops_perms = ["admin:user:update", "admin:user:assign_role", "admin:role:set_permissions"]
    uid = await _new_user(client, admin, perms=ops_perms)  # kai 也有该权限
    _, kai = await _login(client)

    # kai 是第二个管理员时,可以停用 root
    r = await client.put("/api/v1/users/1", json={"is_active": False}, headers=kai)
    assert r.status_code == 200
    # 现在 kai 是最后一个:不能摘掉自己的角色,也不能清空角色权限
    r = await client.put(f"/api/v1/users/{uid}/roles", json={"role_ids": []}, headers=kai)
    assert r.status_code == 409
    r = await client.put("/api/v1/roles/2/permissions", json={"permissions": []}, headers=kai)
    assert r.status_code == 409 and "至少" in r.json()["message"]


async def test_last_admin_cannot_switch_to_powerless_role(client, admin):
    r = await client.post(
        "/api/v1/roles", json={"name": "viewer", "permissions": []}, headers=admin
    )
    r = await client.put(
        "/api/v1/users/1/roles", json={"role_ids": [r.json()["id"]]}, headers=admin
    )
    assert r.status_code == 409 and "至少" in r.json()["message"]


async def test_change_password_is_rate_limited(client, admin):
    await _new_user(client, admin)
    _, kai = await _login(client)
    body = {"old_password": "wrong-pass", "new_password": "newpass123"}
    url = "/api/v1/users/me/password"
    for _ in range(5):
        assert (await client.post(url, json=body, headers=kai)).status_code == 400
    r = await client.post(url, json=body, headers=kai)
    assert r.status_code == 429 and r.json()["code"] == "TOO_MANY_REQUESTS"


async def test_change_own_password(client, admin, redis):
    uid = await _new_user(client, admin)
    _, kai = await _login(client)
    url = "/api/v1/users/me/password"

    r = await client.post(
        url, json={"old_password": "wrong-pass", "new_password": "newpass123"}, headers=kai
    )
    assert r.status_code == 400
    r = await client.post(
        url, json={"old_password": "password123", "new_password": "short"}, headers=kai
    )
    assert r.status_code == 400

    r = await client.post(
        url, json={"old_password": "password123", "new_password": "newpass123"}, headers=kai
    )
    assert r.status_code == 204
    assert f"user_refresh:{uid}" not in redis.data  # 其他设备需要重新登录
    assert (await _login(client))[0].status_code == 401
    assert (await _login(client, password="newpass123"))[0].status_code == 200


async def test_reset_password(client, admin, redis):
    uid = await _new_user(client, admin)
    await _login(client)
    r = await client.post(
        f"/api/v1/users/{uid}/reset-password", json={"new_password": "reset-pass1"}, headers=admin
    )
    assert r.status_code == 204
    assert f"user_refresh:{uid}" not in redis.data
    assert (await _login(client))[0].status_code == 401
    assert (await _login(client, password="reset-pass1"))[0].status_code == 200

    r = await client.post(
        "/api/v1/users/999/reset-password", json={"new_password": "reset-pass1"}, headers=admin
    )
    assert r.status_code == 404


async def test_reset_password_requires_permission(client, admin):
    await _new_user(client, admin)  # kai 没有任何角色
    _, kai = await _login(client)
    r = await client.post(
        "/api/v1/users/1/reset-password", json={"new_password": "reset-pass1"}, headers=kai
    )
    assert r.status_code == 403 and r.json()["detail"] == "admin:user:reset_password"
