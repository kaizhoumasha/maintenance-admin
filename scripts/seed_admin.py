"""写入初始权限、确保 admin 角色拥有表里全部权限,并创建/授权一个管理员用户。可重复执行。

用法: uv run python -m scripts.seed_admin <username> <email>
密码取自环境变量 ADMIN_PASSWORD,未设置则交互式 getpass 输入(不进 shell 历史)。
"""

import asyncio
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import hash_password
from app.core.database import session_factory
from app.models import Permission, Role, User

# 仅是初始数据;之后新权限走 POST /permissions,不必改这里
INITIAL_PERMISSIONS = {
    "admin:user:create": "创建用户",
    "admin:user:list": "查看用户列表",
    "admin:user:read": "查看用户",
    "admin:user:assign_role": "分配用户角色",
    "admin:role:read": "查看角色",
    "admin:role:create": "创建角色",
    "admin:role:set_permissions": "设置角色权限",
    "admin:permission:read": "查看权限",
    "admin:permission:create": "创建权限",
    "admin:audit:read": "查看审计日志",
    "admin:audit:read_archive": "查看归档审计日志",
    "admin:maintenance:archive_audit": "触发审计日志归档",
}


async def seed_admin(session: AsyncSession, username: str, email: str, password: str) -> None:
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
    asyncio.run(main(sys.argv[1], sys.argv[2], password))
