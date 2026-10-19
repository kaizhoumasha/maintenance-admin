import os
import subprocess
import sys

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core import verify_password
from app.models import Menu, Permission, Role, User
from scripts.seed_admin import seed_admin


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("username", "ab"),
        ("username", "a" * 65),
        ("email", "invalid-email"),
        ("email", "a" * 244 + "@example.com"),
        ("password", "a" * 7),
        ("password", "a" * 129),
    ],
)
async def test_invalid_admin_input_does_not_write_seed_data(db, field, value):
    data = {"username": "root", "email": "root@example.com", "password": "password123"}
    data[field] = value
    with pytest.raises(ValidationError):
        await seed_admin(db, **data)
    await db.commit()
    for model in (User, Role, Permission, Menu):
        assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_repeated_seed_preserves_account_and_menu_edits_and_restores_admin_grants(db):
    await seed_admin(db, "root", "root@example.com", "password123")
    user = await db.scalar(select(User).options(selectinload(User.roles)))
    user_id, password_hash = user.id, user.hashed_password
    extra_role = Role(name="observer")
    extra_permission = Permission(code="business:asset:read", name="查看资产")
    db.add_all([extra_role, extra_permission])
    user.roles.append(extra_role)
    user.allow_multi_login = False
    admin = await db.scalar(
        select(Role).where(Role.name == "admin").options(selectinload(Role.permissions))
    )
    admin.permissions = []
    menu = await db.scalar(select(Menu).where(Menu.name == "SystemUser"))
    menu.title = "本地用户菜单"
    await db.commit()
    counts = [
        await db.scalar(select(func.count()).select_from(m)) for m in (User, Role, Permission, Menu)
    ]

    await seed_admin(db, "root", "other@example.com", "different-password")

    user = await db.scalar(
        select(User).options(selectinload(User.roles)).execution_options(populate_existing=True)
    )
    admin = await db.scalar(
        select(Role)
        .where(Role.name == "admin")
        .options(selectinload(Role.permissions))
        .execution_options(populate_existing=True)
    )
    assert user.id == user_id and user.email == "root@example.com"
    assert user.hashed_password == password_hash
    assert verify_password("password123", user.hashed_password)
    assert not verify_password("different-password", user.hashed_password)
    assert user.allow_multi_login is True
    assert {role.name for role in user.roles} == {"admin", "observer"}
    assert {p.code for p in admin.permissions} == set(await db.scalars(select(Permission.code)))
    assert (await db.scalar(select(Menu).where(Menu.name == "SystemUser"))).title == "本地用户菜单"
    assert [
        await db.scalar(select(func.count()).select_from(m)) for m in (User, Role, Permission, Menu)
    ] == counts


def test_invalid_admin_cli_exits_without_echoing_password():
    password = "Secr3t"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.seed_admin", "root", "root@example.com"],
        env={
            **os.environ,
            "ADMIN_PASSWORD": password,
            "DATABASE_URL": "sqlite+aiosqlite://",
            "REDIS_URL": "redis://localhost:1/0",
            "ENVIRONMENT": "local",
            "JWT_SECRET_KEY": "isolated-validation-test-secret-32",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "管理员参数无效" in result.stderr and "password" in result.stderr
    assert password not in result.stdout + result.stderr
