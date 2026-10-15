import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import selectinload

from app.models import Permission, Role, User
from app.services import rbac_service


@pytest.mark.parametrize("target", ["permissions", "roles"])
async def test_concurrent_replacements_do_not_merge_grants(pg_sessions, target):
    suffix = uuid.uuid4().hex
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
        async with pg_sessions() as writer, pg_sessions() as other, pg_sessions() as observer:
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
                async with asyncio.timeout(5):
                    while not pending.done():
                        waiting = await observer.scalar(
                            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                            {"pid": pid},
                        )
                        await observer.rollback()
                        if waiting == "Lock":
                            break
                        await asyncio.sleep(0.01)
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
            await session.commit()
