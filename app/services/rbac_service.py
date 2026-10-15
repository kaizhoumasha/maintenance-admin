from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import AppError, ErrorCode
from app.models import Permission, Role, User


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


async def set_role_permissions(session: AsyncSession, role_id: int, codes: Iterable[str]) -> Role:
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
    await session.commit()
    return role


async def set_user_roles(session: AsyncSession, user_id: int, role_ids: Iterable[int]) -> None:
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
    await session.commit()
