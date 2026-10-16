import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text

from app.core import AppError, ErrorCode
from app.models import Menu, Permission, Role, User
from app.schemas.menu import MenuIn
from app.services import menu_service


def make_menu_in(**kw):
    return MenuIn(**{"name": "Demo", "path": "/demo", "component": "Layout", "title": "演示", **kw})


async def raises_app_error(coro):
    with pytest.raises(AppError) as e:
        await coro
    return e.value


async def test_path_rule_depends_on_level(db):
    assert (
        await raises_app_error(menu_service.create_menu(db, make_menu_in(path="demo")))
    ).code == ErrorCode.INVALID_REQUEST
    parent = await menu_service.create_menu(db, make_menu_in())
    bad_child = make_menu_in(name="Kid", path="/kid", component="x/y", parent_id=parent.id)
    assert (
        await raises_app_error(menu_service.create_menu(db, bad_child))
    ).code == ErrorCode.INVALID_REQUEST


async def test_unknown_parent_and_permission_code_are_400(db):
    err = await raises_app_error(menu_service.create_menu(db, make_menu_in(parent_id=99, path="x")))
    assert err.code == ErrorCode.INVALID_REQUEST and err.message == "父菜单不存在"
    err = await raises_app_error(
        menu_service.create_menu(db, make_menu_in(permission_code="no:such:code"))
    )
    assert err.code == ErrorCode.INVALID_REQUEST and err.message == "权限码不存在"


async def test_duplicate_name_conflicts_but_rename_to_self_is_fine(db):
    first = await menu_service.create_menu(db, make_menu_in())
    err = await raises_app_error(menu_service.create_menu(db, make_menu_in(path="/other")))
    assert err.code == ErrorCode.CONFLICT
    await menu_service.update_menu(db, first.id, make_menu_in(title="改了"))  # 同名更新自己不算冲突


async def test_cannot_move_under_self_or_descendant(db):
    a = await menu_service.create_menu(db, make_menu_in(name="A", path="/a"))
    b = await menu_service.create_menu(
        db, make_menu_in(name="B", path="b", component="x/b", parent_id=a.id)
    )
    for parent_id in (a.id, b.id):
        err = await raises_app_error(
            menu_service.update_menu(db, a.id, make_menu_in(name="A", parent_id=parent_id))
        )
        assert "自己或其子级" in err.message


async def test_delete_requires_no_children(db):
    parent = await menu_service.create_menu(db, make_menu_in())
    child = await menu_service.create_menu(
        db, make_menu_in(name="Kid", path="kid", component="x/y", parent_id=parent.id)
    )
    assert (
        await raises_app_error(menu_service.delete_menu(db, parent.id))
    ).code == ErrorCode.CONFLICT
    await menu_service.delete_menu(db, child.id)
    await menu_service.delete_menu(db, parent.id)
    assert (
        await raises_app_error(menu_service.delete_menu(db, parent.id))
    ).code == ErrorCode.NOT_FOUND


async def _user_with(db, *codes):
    perms = [Permission(code=c, name=c) for c in ("a:b:read", "a:b:write")]
    role = Role(name="r", permissions=[p for p in perms if p.code in codes])
    user = User(username="u", email="u@e.com", hashed_password="x", roles=[role])
    db.add_all([*perms, role, user])
    await db.commit()
    return user


async def test_routes_are_filtered_shaped_and_sorted(db):
    user = await _user_with(db, "a:b:read")
    await menu_service.create_menu(
        db, make_menu_in(name="Home", path="/home", component="home", sort=-1)
    )
    d = await menu_service.create_menu(
        db, make_menu_in(name="Dir", path="/dir", sort=1, icon="folder")
    )
    for name, path, code, hidden in [
        ("Read", "read", "a:b:read", False),
        ("Write", "write", "a:b:write", False),
        ("ReadDetail", "read/:id", "a:b:read", True),
    ]:
        await menu_service.create_menu(
            db,
            make_menu_in(
                name=name,
                path=path,
                component=f"p/{name}",
                parent_id=d.id,
                permission_code=code,
                hidden=hidden,
                keep_alive=name == "Read",
            ),
        )
    routes = await menu_service.get_user_routes(db, user.id)
    assert [r["name"] for r in routes] == ["Home", "Dir"]
    read, detail = routes[1]["children"]
    assert read["meta"] == {
        "title": "演示",
        "icon": None,
        "hidden": None,
        "keepAlive": True,
        "permission": "a:b:read",
    }
    assert detail["meta"]["hidden"] is True  # hidden 照常返回
    assert routes[0]["children"] is None


async def test_directory_without_visible_children_is_dropped(db):
    user = await _user_with(db)  # 没有任何权限
    d = await menu_service.create_menu(db, make_menu_in())
    await menu_service.create_menu(
        db,
        make_menu_in(
            name="Page", path="p", component="p/x", parent_id=d.id, permission_code="a:b:read"
        ),
    )
    assert await menu_service.get_user_routes(db, user.id) == []


async def test_concurrent_moves_cannot_form_cycle(pg_sessions, wait_for_lock, monkeypatch):
    suffix = uuid.uuid4().hex
    async with pg_sessions() as session:
        a = await menu_service.create_menu(session, make_menu_in(name=f"A{suffix}", path="/a"))
        b = await menu_service.create_menu(session, make_menu_in(name=f"B{suffix}", path="/b"))
    ready, release = asyncio.Event(), asyncio.Event()
    validate = menu_service._validate
    tasks = []
    try:
        async with pg_sessions() as writer, pg_sessions() as other:
            # 第二个会话先缓存旧的祖先链;等待锁之后必须重新读取。
            cached = list(await other.scalars(select(Menu).where(Menu.id.in_([a.id, b.id]))))
            assert all(m.parent_id is None for m in cached)
            pid = await other.scalar(text("SELECT pg_backend_pid()"))

            async def pause_first(session, data, self_id=None):
                if session is writer:
                    try:
                        await validate(session, data, self_id)
                    finally:
                        ready.set()
                    await release.wait()
                else:
                    await validate(session, data, self_id)

            monkeypatch.setattr(menu_service, "_validate", pause_first)
            tasks.append(
                asyncio.create_task(
                    menu_service.update_menu(
                        writer, a.id, make_menu_in(name=a.name, path="a", parent_id=b.id)
                    )
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), 5)
                tasks.append(
                    asyncio.create_task(
                        menu_service.update_menu(
                            other, b.id, make_menu_in(name=b.name, path="b", parent_id=a.id)
                        )
                    )
                )
                await wait_for_lock(tasks[1], pid)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], Menu)
        assert isinstance(results[1], AppError)
        assert results[1].code == ErrorCode.INVALID_REQUEST
        async with pg_sessions() as session:
            assert (await session.get(Menu, a.id)).parent_id == b.id
            assert (await session.get(Menu, b.id)).parent_id is None
    finally:
        async with pg_sessions() as session:
            await session.execute(delete(Menu).where(Menu.id.in_([a.id, b.id])))
            await session.commit()
