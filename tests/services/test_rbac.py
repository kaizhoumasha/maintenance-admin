import asyncio

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core import AppError, ErrorCode
from app.core.identifiers import uuid7
from app.database import SCHEMA
from app.models import Base, Permission, Role, User, role_permission, user_role
from app.schemas.user import UserUpdate
from app.services import rbac_service, user_service
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


@pytest.fixture
async def admin_pair(pg_sessions):
    # 独立 schema 保证恰好两位管理员,不被其他测试留下的种子管理员掩盖。
    schema = "admin_guard_" + uuid7().hex
    engine = pg_sessions.kw["bind"].execution_options(schema_translate_map={SCHEMA: schema})
    async with engine.begin() as connection:
        await connection.execute(CreateSchema(schema))
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[
                    User.__table__,
                    Role.__table__,
                    Permission.__table__,
                    user_role,
                    role_permission,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            permission = Permission(code="admin:role:set_permissions", name="manage")
            roles = [Role(name=f"guard_{i}", permissions=[permission]) for i in range(2)]
            users = [
                User(
                    username=f"guard_{i}",
                    email=f"guard_{i}@example.test",
                    hashed_password="x",
                    roles=[role],
                )
                for i, role in enumerate(roles)
            ]
            session.add_all(users)
            await session.commit()
            yield factory, [u.id for u in users], [r.id for r in roles]
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True))


@pytest.mark.parametrize(
    "actions",
    [
        ("roles", "roles"),
        ("permissions", "permissions"),
        ("disable", "roles"),
        ("roles", "disable"),
        ("disable", "permissions"),
        ("permissions", "disable"),
        ("disable", "disable"),
    ],
)
async def test_different_admin_changes_preserve_one_admin(
    admin_pair, wait_for_lock, redis, actions
):
    factory, users, roles = admin_pair
    reached, resume = asyncio.Event(), asyncio.Event()
    async with factory() as first, factory() as second:
        commit = first.commit

        async def paused_commit():
            reached.set()
            await asyncio.wait_for(resume.wait(), 5)
            await commit()

        first.commit = paused_commit

        async def change(session, index, action):
            try:
                if action == "roles":
                    await rbac_service.set_user_roles(session, users[index], [])
                elif action == "permissions":
                    await rbac_service.set_role_permissions(session, roles[index], [])
                else:
                    await user_service.update_user(
                        session, redis, users[1 - index], users[index], UserUpdate(is_active=False)
                    )
                return "ok"
            except AppError as error:
                await session.rollback()
                return error.code

        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        pending = [asyncio.create_task(change(first, 0, actions[0]))]
        try:
            await asyncio.wait_for(reached.wait(), 5)
            pending.append(asyncio.create_task(change(second, 1, actions[1])))
            await wait_for_lock(pending[1], pid)
            resume.set()
            assert await asyncio.wait_for(asyncio.gather(*pending), 5) == ["ok", ErrorCode.CONFLICT]
            # 冲突已回滚,同一会话的下一笔合法事务仍可取得保护锁并完成。
            await rbac_service.set_user_roles(second, users[1], [roles[1]])
        finally:
            resume.set()
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    async with factory() as session:
        admins = list(
            await session.scalars(
                select(User.id)
                .join(User.roles)
                .join(Role.permissions)
                .where(User.is_active.is_(True), Permission.code == "admin:role:set_permissions")
            )
        )
        assert admins == [users[1]]


async def test_disabling_reloads_current_user_state(admin_pair, redis):
    factory, users, _ = admin_pair
    async with factory() as stale, factory() as writer:
        cached = await stale.get(User, users[1])
        assert cached.email == "guard_1@example.test"
        current = await writer.get(User, users[1])
        current.email = "changed@example.test"
        await writer.commit()
        updated = await user_service.update_user(
            stale, redis, users[0], users[1], UserUpdate(is_active=False)
        )
        assert updated.email == "changed@example.test" and not updated.is_active


async def test_profile_edit_does_not_wait_for_admin_change(admin_pair, redis):
    factory, users, _ = admin_pair
    reached, resume = asyncio.Event(), asyncio.Event()
    async with factory() as guarded, factory() as ordinary:
        commit = guarded.commit

        async def paused_commit():
            reached.set()
            await asyncio.wait_for(resume.wait(), 5)
            await commit()

        guarded.commit = paused_commit
        pending = asyncio.create_task(rbac_service.set_user_roles(guarded, users[0], []))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            updated = await asyncio.wait_for(
                user_service.update_user(
                    ordinary, redis, users[0], users[1], UserUpdate(email="profile@example.test")
                ),
                2,
            )
            assert updated.email == "profile@example.test"
        finally:
            resume.set()
            await asyncio.wait_for(pending, 5)


async def test_role_delete_cannot_remove_a_concurrently_assigned_last_admin(
    admin_pair, wait_for_lock
):
    factory, users, roles = admin_pair
    async with factory.begin() as session:
        await session.execute(delete(User).where(User.id == users[0]))
    reached, resume = asyncio.Event(), asyncio.Event()
    async with factory() as writer, factory() as deleter:
        commit = writer.commit

        async def paused_commit():
            reached.set()
            await asyncio.wait_for(resume.wait(), 5)
            await commit()

        writer.commit = paused_commit
        pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        assigning = asyncio.create_task(rbac_service.set_user_roles(writer, users[1], [roles[0]]))
        deleting = None
        try:
            await asyncio.wait_for(reached.wait(), 5)
            deleting = asyncio.create_task(rbac_service.delete_role(deleter, roles[0]))
            await wait_for_lock(deleting, pid)
            resume.set()
            await asyncio.wait_for(assigning, 5)
            with pytest.raises(AppError) as caught:
                await asyncio.wait_for(deleting, 5)
            assert caught.value.code == ErrorCode.CONFLICT
        finally:
            resume.set()
            tasks = [task for task in (assigning, deleting) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    async with factory() as session:
        assert await rbac_service.get_user_permission_codes(session, users[1]) == [
            "admin:role:set_permissions"
        ]
