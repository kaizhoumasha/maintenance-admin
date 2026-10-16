import pytest

from app.core.database import get_session
from app.core.security import create_access_token
from app.main import app
from scripts.seed_admin import INITIAL_MENUS, INITIAL_PERMISSIONS, seed_admin

MENUS = "/api/v1/menus"
ME = "/api/v1/users/me/menus"

SYSTEM = {
    "name": "System",
    "path": "/system",
    "component": "Layout",
    "meta": {"title": "系统管理", "icon": "setting"},
}
USER_PAGE = {
    "name": "SystemUser",
    "path": "user",
    "component": "system/user/index",
    "meta": {"title": "用户管理", "keepAlive": True, "permission": "admin:user:list"},
}
USER_DETAIL = {
    "name": "SystemUserDetail",
    "path": "user/:id",
    "component": "system/user/detail",
    "meta": {"title": "用户详情", "hidden": True, "permission": "admin:user:read"},
}


def _body(**kw):
    return {"name": "Demo", "path": "/demo", "component": "Layout", "title": "演示", **kw}


def _find(nodes, name):
    for n in nodes:
        if n["name"] == name:
            return n
        if hit := _find(n.get("children") or [], name):
            return hit


def _names(nodes):
    return [x for n in nodes for x in [n["name"], *_names(n.get("children") or [])]]


async def _headers_with(client, admin, perms, name="kai"):
    body = {"username": name, "email": f"{name}@example.com", "password": "password123"}
    uid = (await client.post("/api/v1/users", json=body, headers=admin)).json()["id"]
    if perms:
        r = await client.post(
            "/api/v1/roles", json={"name": f"r-{name}", "permissions": perms}, headers=admin
        )
        await client.put(
            f"/api/v1/users/{uid}/roles", json={"role_ids": [r.json()["id"]]}, headers=admin
        )
    return {"Authorization": f"Bearer {create_access_token(str(uid))}"}


# ---- 管理端 ----


async def test_admin_tree_contains_seeded_menus(client, admin):
    tree = (await client.get(MENUS, headers=admin)).json()
    system = _find(tree, "System")
    assert [c["name"] for c in system["children"]][:2] == ["SystemUser", "SystemUserDetail"]
    page = _find(tree, "SystemUser")
    assert page["permission_code"] == "admin:user:list" and page["keep_alive"] is True
    assert page["parent_id"] == system["id"] and "id" in page


async def test_create_update_delete_menu(client, admin):
    top = await client.post(MENUS, json=_body(), headers=admin)
    assert top.status_code == 201
    pid = top.json()["id"]
    child = await client.post(
        MENUS,
        json=_body(name="DemoList", path="list", component="demo/list", parent_id=pid),
        headers=admin,
    )
    assert child.status_code == 201
    cid = child.json()["id"]
    assert _find((await client.get(MENUS, headers=admin)).json(), "DemoList")["parent_id"] == pid

    r = await client.put(
        f"{MENUS}/{cid}",
        json=_body(name="DemoList", path="list", component="demo/list", parent_id=pid, title="新"),
        headers=admin,
    )
    assert r.status_code == 200 and r.json()["title"] == "新"

    assert (await client.delete(f"{MENUS}/{pid}", headers=admin)).status_code == 409  # 有子菜单
    assert (await client.delete(f"{MENUS}/{cid}", headers=admin)).status_code == 204
    assert (await client.delete(f"{MENUS}/{pid}", headers=admin)).status_code == 204
    assert (await client.delete(f"{MENUS}/{pid}", headers=admin)).status_code == 404
    assert (await client.put(f"{MENUS}/999", json=_body(), headers=admin)).status_code == 404


@pytest.mark.parametrize(
    "override",
    [
        {"path": "demo"},  # 顶级必须以 / 开头
        {"parent_id": 999},  # 父菜单不存在
        {"permission_code": "no:such:code"},
        {"component": ""},
        {"name": "bad name"},
        {"title": ""},
    ],
)
async def test_create_menu_validation(client, admin, override):
    r = await client.post(MENUS, json=_body(**override), headers=admin)
    assert r.status_code == 400 and r.json()["code"] == "INVALID_REQUEST"


async def test_child_path_must_be_relative(client, admin):
    pid = (await client.post(MENUS, json=_body(), headers=admin)).json()["id"]
    r = await client.post(
        MENUS, json=_body(name="Kid", path="/kid", component="x/y", parent_id=pid), headers=admin
    )
    assert r.status_code == 400


async def test_duplicate_name_conflicts(client, admin):
    await client.post(MENUS, json=_body(), headers=admin)
    r = await client.post(MENUS, json=_body(path="/other"), headers=admin)
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


async def test_cannot_move_menu_under_itself_or_descendant(client, admin):
    a = (await client.post(MENUS, json=_body(name="A", path="/a"), headers=admin)).json()["id"]
    b = (
        await client.post(
            MENUS,
            json=_body(name="B", path="b", component="x/b", parent_id=a),
            headers=admin,
        )
    ).json()["id"]
    for parent in (a, b):
        r = await client.put(
            f"{MENUS}/{a}", json=_body(name="A", path="/a", parent_id=parent), headers=admin
        )
        assert r.status_code == 400 and "自己或其子级" in r.json()["message"]


async def test_menu_admin_endpoints_need_permission(client, admin):
    h = await _headers_with(client, admin, ["admin:menu:read"])
    assert (await client.get(MENUS, headers=h)).status_code == 200
    r = await client.post(MENUS, json=_body(), headers=h)
    assert r.status_code == 403 and r.json()["detail"] == "admin:menu:create"


# ---- 当前用户的路由 ----


async def test_my_menus_filtered_by_permission(client, admin):
    h = await _headers_with(client, admin, ["admin:user:list"])
    assert (await client.get(ME, headers=h)).json() == [{**SYSTEM, "children": [USER_PAGE]}]

    # 隐藏的详情页:有权限时仍然返回(路由必须注册),带 meta.hidden
    h2 = await _headers_with(client, admin, ["admin:user:list", "admin:user:read"], name="lee")
    assert (await client.get(ME, headers=h2)).json() == [
        {**SYSTEM, "children": [USER_PAGE, USER_DETAIL]}
    ]


async def test_my_menus_empty_without_permission(client, admin):
    h = await _headers_with(client, admin, [])
    assert (await client.get(ME, headers=h)).json() == []
    assert (await client.get(ME)).status_code == 401


async def test_my_menus_public_menu_and_order(client, admin):
    await client.post(
        MENUS,
        json=_body(name="Home", path="/home", component="home/index", title="首页", sort=0),
        headers=admin,
    )
    h = await _headers_with(client, admin, ["admin:user:list"])
    names = [n["name"] for n in (await client.get(ME, headers=h)).json()]
    assert names == ["Home", "System"]  # 无权限码的菜单对所有登录用户可见;按 sort 升序


async def test_admin_sees_all_seeded_routes(client, admin):
    routes = (await client.get(ME, headers=admin)).json()
    assert set(_names(routes)) == {m["name"] for m in INITIAL_MENUS}


# ---- 种子数据 ----


async def test_seed_menus_idempotent_and_keeps_edits(client, admin):
    page = _find((await client.get(MENUS, headers=admin)).json(), "SystemUser")
    edited = {
        k: page[k]
        for k in (
            "parent_id",
            "name",
            "path",
            "component",
            "redirect",
            "icon",
            "sort",
            "hidden",
            "keep_alive",
            "permission_code",
        )
    }
    await client.put(f"{MENUS}/{page['id']}", json={**edited, "title": "改过"}, headers=admin)
    before = _names((await client.get(MENUS, headers=admin)).json())

    gen = app.dependency_overrides[get_session]()
    session = await anext(gen)
    await seed_admin(session, "root", "root@example.com", "password123")
    await gen.aclose()

    tree = (await client.get(MENUS, headers=admin)).json()
    assert _names(tree) == before
    assert _find(tree, "SystemUser")["title"] == "改过"


def test_initial_menus_are_consistent():
    names = [m["name"] for m in INITIAL_MENUS]
    assert len(names) == len(set(names))
    for i, m in enumerate(INITIAL_MENUS):
        assert m.get("parent") is None or m["parent"] in names[:i]  # 父级排在前面
        assert m.get("permission") in (None, *INITIAL_PERMISSIONS)
