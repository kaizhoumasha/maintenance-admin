"""写入初始权限、确保 admin 角色拥有表里全部权限,并创建/授权一个管理员用户。可重复执行。

用法: uv run python -m scripts.seed_admin <username> <email>
密码取自环境变量 ADMIN_PASSWORD,未设置则交互式 getpass 输入(不进 shell 历史)。
"""

import asyncio
import getpass
import os
import sys

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import hash_password
from app.core.database import session_factory
from app.models import Menu, Permission, Role, User
from app.schemas.user import UserCreate

# 仅是初始数据;之后新权限走 POST /permissions,不必改这里
INITIAL_PERMISSIONS = {
    "dev:ecs:send_real": "真实 ECS 联调发送",
    "admin:user:create": "创建用户",
    "admin:user:list": "查看用户列表",
    "admin:user:read": "查看用户",
    "admin:user:assign_role": "分配用户角色",
    "admin:user:update": "修改用户",
    "admin:user:reset_password": "重置用户密码",
    "admin:role:read": "查看角色",
    "admin:role:create": "创建角色",
    "admin:role:set_permissions": "设置角色权限",
    "admin:role:update": "修改角色",
    "admin:role:delete": "删除角色",
    "admin:permission:read": "查看权限",
    "admin:permission:create": "创建权限",
    "admin:audit:read": "查看审计日志",
    "admin:audit:read_archive": "查看归档审计日志",
    "admin:maintenance:archive_audit": "触发审计日志归档",
    "admin:menu:read": "查看菜单",
    "admin:menu:create": "创建菜单",
    "admin:menu:update": "修改菜单",
    "admin:menu:delete": "删除菜单",
}

# 初始菜单:按 name 幂等写入,已存在的不覆盖(管理员可能改过);parent 引用上级的 name,须排在上级之后
_SYSTEM = "System"
INITIAL_MENUS = [
    {
        "name": _SYSTEM,
        "path": "/system",
        "component": "Layout",
        "title": "系统管理",
        "icon": "setting",
        "sort": 10,
    },
    {
        "name": "SystemUser",
        "parent": _SYSTEM,
        "path": "user",
        "component": "system/user/index",
        "title": "用户管理",
        "keep_alive": True,
        "sort": 10,
        "permission": "admin:user:list",
    },
    {
        "name": "SystemUserDetail",
        "parent": _SYSTEM,
        "path": "user/:id",
        "component": "system/user/detail",
        "title": "用户详情",
        "hidden": True,
        "sort": 11,
        "permission": "admin:user:read",
    },
    {
        "name": "SystemRole",
        "parent": _SYSTEM,
        "path": "role",
        "component": "system/role/index",
        "title": "角色管理",
        "sort": 20,
        "permission": "admin:role:read",
    },
    {
        "name": "SystemPermission",
        "parent": _SYSTEM,
        "path": "permission",
        "component": "system/permission/index",
        "title": "权限管理",
        "sort": 30,
        "permission": "admin:permission:read",
    },
    {
        "name": "SystemMenu",
        "parent": _SYSTEM,
        "path": "menu",
        "component": "system/menu/index",
        "title": "菜单管理",
        "sort": 40,
        "permission": "admin:menu:read",
    },
    {
        "name": "SystemAuditLog",
        "parent": _SYSTEM,
        "path": "audit-log",
        "component": "system/audit-log/index",
        "title": "审计日志",
        "sort": 50,
        "permission": "admin:audit:read",
    },
]


async def seed_admin(session: AsyncSession, username: str, email: str, password: str) -> None:
    UserCreate(username=username, email=email, password=password)
    existing = {p.code: p for p in await session.scalars(select(Permission))}
    for code, perm_name in INITIAL_PERMISSIONS.items():
        if code not in existing:
            existing[code] = Permission(code=code, name=perm_name)
            session.add(existing[code])
    role = await session.scalar(
        select(Role).where(Role.name == "admin").options(selectinload(Role.permissions))
    )
    if role is None:
        role = Role(name="admin")
        session.add(role)
    role.permissions = list(existing.values())

    menus = {m.name: m for m in await session.scalars(select(Menu))}
    for item in INITIAL_MENUS:
        if item["name"] in menus:
            continue
        fields = {k: v for k, v in item.items() if k not in ("parent", "permission")}
        parent = menus.get(item.get("parent"))
        menus[item["name"]] = Menu(
            **fields,
            parent_id=parent.id if parent else None,
            permission_code=item.get("permission"),
        )
        session.add(menus[item["name"]])
        await session.flush()  # 后面的子菜单要用到它的 id

    user = await session.scalar(
        select(User).where(User.username == username).options(selectinload(User.roles))
    )
    if user is None:
        user = User(
            username=username,
            email=email,
            hashed_password=await asyncio.to_thread(hash_password, password),
        )
        session.add(user)
    user.allow_multi_login = True
    if role not in user.roles:
        user.roles.append(role)
    await session.commit()


async def main(username: str, email: str, password: str) -> None:
    async with session_factory() as session:
        await seed_admin(session, username, email, password)


if __name__ == "__main__":
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2], password))
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        sys.exit(f"管理员参数无效: {reasons}")
