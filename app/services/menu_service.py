from collections import defaultdict
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import AppError, ErrorCode
from app.core.deps import user_is_admin
from app.models import Menu, Permission
from app.schemas.menu import MenuIn, MenuRead
from app.services import rbac_service

LAYOUT = "Layout"  # component 取此值表示目录(布局壳)


def _build(menus: list[Menu]) -> list[dict[str, Any]]:
    """按 sort、id 排序后组装成 [{"menu": Menu, "children": [...]}] 树。"""
    by_parent = defaultdict(list)
    for m in sorted(menus, key=lambda m: (m.sort, m.id)):
        by_parent[m.parent_id].append(m)

    def level(parent_id: int | None) -> list[dict[str, Any]]:
        return [{"menu": m, "children": level(m.id)} for m in by_parent[parent_id]]

    return level(None)


def _to_admin(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**MenuRead.model_validate(n["menu"]).model_dump(), "children": _to_admin(n["children"])}
        for n in nodes
    ]


def _to_routes(nodes: list[dict[str, Any]], codes: set[str]) -> list[dict[str, Any]]:
    """无权限的菜单连同子树一起丢弃;目录没有可见子项时整体丢弃。

    hidden 的菜单照常返回(路由必须注册才能跳转),由前端据 meta.hidden 决定是否进侧边栏。
    """
    routes = []
    for n in nodes:
        m: Menu = n["menu"]
        if m.permission_code and m.permission_code not in codes:
            continue
        children = _to_routes(n["children"], codes)
        if m.component == LAYOUT and not children:
            continue
        routes.append(
            {
                "name": m.name,
                "path": m.path,
                "component": m.component,
                "redirect": m.redirect,
                "meta": {
                    "title": m.title,
                    "icon": m.icon,
                    "hidden": m.hidden or None,
                    "keepAlive": m.keep_alive or None,
                    "permission": m.permission_code,
                },
                "children": children or None,
            }
        )
    return routes


async def _all_menus(session: AsyncSession) -> list[Menu]:
    return list(await session.scalars(select(Menu)))


async def list_menu_tree(session: AsyncSession) -> list[dict[str, Any]]:
    return _to_admin(_build(await _all_menus(session)))


async def get_user_routes(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    codes = set(await rbac_service.get_user_permission_codes(session, user_id))
    if not await user_is_admin(session, user_id):
        codes -= {"asset:device:read", "asset:workline:read"}
    return _to_routes(_build(await _all_menus(session)), codes)


async def _get(session: AsyncSession, menu_id: int) -> Menu:
    menu = await session.get(Menu, menu_id)
    if menu is None:
        raise AppError(ErrorCode.NOT_FOUND, "菜单不存在")
    return menu


async def _validate(session: AsyncSession, data: MenuIn, self_id: int | None = None) -> None:
    parent_id = data.parent_id
    while parent_id is not None:
        parent = await session.get(Menu, parent_id, populate_existing=True)
        if parent is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "父菜单不存在", str(parent_id))
        if parent.id == self_id:  # 沿祖先链走到自己:父级是自己或自己的后代
            raise AppError(ErrorCode.INVALID_REQUEST, "不能把菜单移到自己或其子级下")
        parent_id = parent.parent_id

    # vue-router:顶级 path 以 / 开头,子级是相对路径
    if data.parent_id is None and not data.path.startswith("/"):
        raise AppError(ErrorCode.INVALID_REQUEST, "顶级菜单的 path 必须以 / 开头", data.path)
    if data.parent_id is not None and data.path.startswith("/"):
        raise AppError(ErrorCode.INVALID_REQUEST, "子级菜单的 path 不能以 / 开头", data.path)

    code = data.permission_code
    if code and not await session.scalar(select(Permission.id).where(Permission.code == code)):
        raise AppError(ErrorCode.INVALID_REQUEST, "权限码不存在", code)

    same_name = select(Menu.id).where(Menu.name == data.name, Menu.id != self_id)
    if await session.scalar(same_name):
        raise AppError(ErrorCode.CONFLICT, "菜单名已存在")


async def create_menu(session: AsyncSession, data: MenuIn) -> Menu:
    await _validate(session, data)
    menu = Menu(**data.model_dump())
    session.add(menu)
    await session.commit()
    return menu


async def update_menu(session: AsyncSession, menu_id: int, data: MenuIn) -> Menu:
    if session.get_bind().dialect.name == "postgresql":
        # 读取祖先链前串行化菜单写入,防止两个移动各自通过校验后形成环。
        await session.execute(text("LOCK TABLE menu IN SHARE ROW EXCLUSIVE MODE"))
    menu = await _get(session, menu_id)
    await _validate(session, data, menu.id)
    for field, value in data.model_dump().items():
        setattr(menu, field, value)
    await session.commit()
    return menu


async def delete_menu(session: AsyncSession, menu_id: int) -> None:
    menu = await _get(session, menu_id)
    if await session.scalar(select(Menu.id).where(Menu.parent_id == menu_id).limit(1)):
        raise AppError(ErrorCode.CONFLICT, "存在子菜单,请先删除子菜单")
    await session.delete(menu)
    await session.commit()
