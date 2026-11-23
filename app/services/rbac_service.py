from collections.abc import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import AppError, ErrorCode
from app.core.deps import user_is_admin
from app.models import Permission, Role, User, role_permission, user_role

# 系统里至少要有一个活跃用户能设置角色权限,否则没人能再恢复权限
ADMIN_GUARD_PERMISSION = "admin:role:set_permissions"
_ADMIN_GUARD_LOCK = 0x41444D494E


async def lock_admin_changes(session: AsyncSession) -> None:
    """授权变更入口先互斥,再加目标行锁/修改;锁随事务结束释放。"""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMIN_GUARD_LOCK}
        )


async def ensure_admin_remains(session: AsyncSession) -> None:
    """持有管理员变更锁后检查未提交改动;失去最后管理员时抛 409,调用方回滚。"""
    stmt = (
        select(func.count(User.id.distinct()))
        .join(user_role, user_role.c.user_id == User.id)
        .join(role_permission, role_permission.c.role_id == user_role.c.role_id)
        .join(Permission, Permission.id == role_permission.c.permission_id)
        .where(User.is_active.is_(True), Permission.code == ADMIN_GUARD_PERMISSION)
    )
    builtin = await session.scalar(select(Role.id).where(Role.name == "admin"))
    if builtin is not None:
        # Delegated setters cannot recover the built-in role or assign it to a new admin.
        members = select(user_role.c.user_id).where(user_role.c.role_id == builtin)
        stmt = stmt.where(User.id.in_(members))
    if not await session.scalar(stmt):
        raise AppError(ErrorCode.CONFLICT, "至少需要保留一个可设置角色权限的活跃用户")


async def _get_permissions(session: AsyncSession, codes: Iterable[str]) -> list[Permission]:
    codes = set(codes)
    perms = list(await session.scalars(select(Permission).where(Permission.code.in_(codes))))
    if len(perms) != len(codes):
        missing = codes - {p.code for p in perms}
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "权限码不存在,请先通过 POST /permissions 创建",
            ", ".join(sorted(missing)),
        )
    return perms


async def get_user_permission_codes(session: AsyncSession, user_id: int) -> list[str]:
    """用户经所有角色获得的权限码,去重并排序。"""
    stmt = (
        select(Permission.code)
        .join(role_permission, role_permission.c.permission_id == Permission.id)
        .join(user_role, user_role.c.role_id == role_permission.c.role_id)
        .where(user_role.c.user_id == user_id)
        .distinct()
        .order_by(Permission.code)
    )
    return list(await session.scalars(stmt))


async def authorize_management(
    session: AsyncSession,
    actor: User,
    permission: str,
    *,
    user_id: int | None = None,
    role_ids: Iterable[int] = (),
    codes: Iterable[str] = (),
    reserved_role: bool = False,
) -> None:
    await lock_admin_changes(session)
    # The actor or their permissions can have changed while waiting for the write lock.
    version = await session.scalar(
        select(User.session_version).where(User.id == actor.id, User.is_active.is_(True))
    )
    if version != actor.session_version:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    available = set(await get_user_permission_codes(session, actor.id))
    if permission not in available:
        raise AppError(ErrorCode.FORBIDDEN, "没有访问权限", detail=permission)
    if await user_is_admin(session, actor.id):
        return

    roles = set(role_ids)
    if user_id is not None:
        roles.update(
            await session.scalars(select(user_role.c.role_id).where(user_role.c.user_id == user_id))
        )
    requested = set(codes)
    if roles:
        grants = await session.execute(
            select(Role.name, Permission.code)
            .select_from(Role)
            .outerjoin(role_permission, role_permission.c.role_id == Role.id)
            .outerjoin(Permission, Permission.id == role_permission.c.permission_id)
            .where(Role.id.in_(roles))
        )
        for name, code in grants:
            reserved_role |= name == "admin"
            if code is not None:
                requested.add(code)
    if reserved_role or not requested <= available:
        raise AppError(ErrorCode.FORBIDDEN, "超出授权管理范围")


async def list_permissions(session: AsyncSession) -> list[Permission]:
    return list(await session.scalars(select(Permission).order_by(Permission.id)))


async def create_permission(session: AsyncSession, code: str, name: str) -> Permission:
    if await session.scalar(select(Permission).where(Permission.code == code)):
        raise AppError(ErrorCode.CONFLICT, "权限码已存在")
    perm = Permission(code=code, name=name)
    session.add(perm)
    await session.commit()
    return perm


async def list_roles(session: AsyncSession) -> list[Role]:
    stmt = select(Role).options(selectinload(Role.permissions)).order_by(Role.id)
    return list(await session.scalars(stmt))


async def create_role(session: AsyncSession, name: str, codes: Iterable[str]) -> Role:
    if await session.scalar(select(Role).where(Role.name == name)):
        raise AppError(ErrorCode.CONFLICT, "角色名已存在")
    role = Role(name=name, permissions=await _get_permissions(session, codes))
    session.add(role)
    await session.commit()
    return role


async def _get_role(session: AsyncSession, role_id: int) -> Role:
    stmt = select(Role).where(Role.id == role_id).options(selectinload(Role.permissions))
    role = await session.scalar(stmt)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "角色不存在")
    return role


async def update_role(session: AsyncSession, role_id: int, name: str) -> Role:
    role = await _get_role(session, role_id)
    if name != role.name and "admin" in (role.name, name):
        raise AppError(ErrorCode.CONFLICT, "admin 是系统保留角色，不能通过改名更改")
    if name != role.name and await session.scalar(select(Role.id).where(Role.name == name)):
        raise AppError(ErrorCode.CONFLICT, "角色名已存在")
    role.name = name
    await session.commit()
    return role


async def delete_role(session: AsyncSession, role_id: int) -> None:
    await lock_admin_changes(session)
    role = await _get_role(session, role_id)
    in_use = select(func.count()).select_from(user_role).where(user_role.c.role_id == role_id)
    if await session.scalar(in_use):
        raise AppError(ErrorCode.CONFLICT, "角色仍有关联用户,请先解除")
    await session.delete(role)
    await session.commit()


async def set_role_permissions(session: AsyncSession, role_id: int, codes: Iterable[str]) -> Role:
    await lock_admin_changes(session)
    stmt = (
        select(Role)
        .where(Role.id == role_id)
        .with_for_update()
        .options(selectinload(Role.permissions))
        .execution_options(populate_existing=True)
    )
    role = await session.scalar(stmt)
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "角色不存在")
    role.permissions = await _get_permissions(session, codes)
    await ensure_admin_remains(session)
    await session.commit()
    return role


async def set_user_roles(session: AsyncSession, user_id: int, role_ids: Iterable[int]) -> None:
    await lock_admin_changes(session)
    stmt = (
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .options(selectinload(User.roles))
        .execution_options(populate_existing=True)
    )
    user = await session.scalar(stmt)
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "用户不存在")
    ids = set(role_ids)
    roles = list(await session.scalars(select(Role).where(Role.id.in_(ids))))
    if len(roles) != len(ids):
        # 与 _get_permissions 一致:请求体里引用了不存在的对象 → 400,路径里的对象不存在才是 404
        missing = ids - {r.id for r in roles}
        raise AppError(
            ErrorCode.INVALID_REQUEST, "角色不存在", ", ".join(map(str, sorted(missing)))
        )
    user.roles = roles
    await ensure_admin_remains(session)
    await session.commit()
