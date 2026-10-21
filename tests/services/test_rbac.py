import asyncio

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import selectinload

from app.core.identifiers import uuid7
from app.models import Permission, Role, User
from app.services import rbac_service
from scripts.seed_admin import seed_admin


@pytest.mark.parametrize("target", ["permissions", "roles"])
async def test_concurrent_replacements_do_not_merge_grants(pg_sessions, wait_for_lock, target):
    suffix = uuid7().hex
    admin_name = f"admin_{suffix[:16]}"
    async with pg_sessions() as session:
        # 保留管理员检查要求库里至少有一个活跃管理员
        await seed_admin(session, admin_name, f"{admin_name}@e.com", "password123")
    async with pg_sessions() as session:
        if target == "permissions":
            first, second = [
                Permission(code=f"test:{suffix}:{name}", name=name) for name in ("a", "b")
            ]
            parent = Role(name=suffix, permissions=[])
            model, relation, update = Role, Role.permissions, rbac_service.set_role_permissions
        else:
            first, second = [Role(name=f"{suffix}_{name}", permissions=[]) for name in ("a", "b")]
            parent = User(username=suffix, email=f"{suffix}@e.com", hashed_password="x", roles=[])
            model, relation, update = User, User.roles, rbac_service.set_user_roles
        session.add_all([parent, first, second])
        await session.commit()
    try:
        async with pg_sessions() as writer, pg_sessions() as other:
            locked = await writer.scalar(
                select(model)
                .where(model.id == parent.id)
                .with_for_update()
                .options(selectinload(relation))
            )
            pid = await other.scalar(text("SELECT pg_backend_pid()"))
            values = [second.code] if target == "permissions" else [second.id]
            pending = asyncio.create_task(update(other, parent.id, values))
            try:
                await wait_for_lock(pending, pid)
                setattr(locked, relation.key, [first])
                await writer.commit()
                await asyncio.wait_for(pending, 5)
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        async with pg_sessions() as session:
            result = await session.scalar(
                select(model).where(model.id == parent.id).options(selectinload(relation))
            )
            assert [item.id for item in getattr(result, relation.key)] == [second.id]
    finally:
        async with pg_sessions() as session:
            await session.execute(delete(model).where(model.id == parent.id))
            child = Permission if target == "permissions" else Role
            await session.execute(delete(child).where(child.id.in_([first.id, second.id])))
            await session.execute(delete(User).where(User.username == admin_name))
            await session.commit()
