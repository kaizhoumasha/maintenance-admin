import asyncio
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import AppError, ErrorCode, hash_password, verify_password
from app.models import User
from app.schemas.base import PageParams
from app.schemas.user import UserCreate, UserMeRead, UserRead, UserUpdate
from app.services import rbac_service, session_service
from app.services.pagination import paginate

# 用户不存在时也跑一次哈希，避免用时差暴露"这个用户名是否存在"
_DUMMY_HASH = "$".join(["0" * 32, "0" * 64])


def _bad_credentials() -> AppError:
    return AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")


async def lock_user(session: AsyncSession, user_id: int) -> User | None:
    """加行锁并强制重读,避免沿用会话里的旧缓存;与改密、登录、刷新共用同一把锁。"""
    return await session.get(User, user_id, with_for_update=True, populate_existing=True)


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    """校验凭证,返回已加行锁的用户(锁持有到请求会话关闭,覆盖 token 签发)。

    验密在锁外进行,错误密码不占锁;通过后加锁,并复核用户仍启用、密码未被改过。
    """
    user = await session.scalar(select(User).where(User.username == username))
    if user is None:
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        raise _bad_credentials()
    user_id, password_hash = user.id, user.hashed_password  # 锁内刷新 ORM 对象会覆盖它们
    valid = await asyncio.to_thread(verify_password, password, password_hash)
    if not valid or not user.is_active:
        raise _bad_credentials()
    user = await lock_user(session, user_id)
    if user is None or not user.is_active or user.hashed_password != password_hash:
        raise _bad_credentials()
    return user


async def get_user(session: AsyncSession, user_id: int, *, for_update: bool = False) -> User:
    user = await (lock_user(session, user_id) if for_update else session.get(User, user_id))
    if not user:
        raise AppError(ErrorCode.NOT_FOUND, "用户不存在")
    return user


async def get_me(session: AsyncSession, user: User) -> UserMeRead:
    codes = await rbac_service.get_user_permission_codes(session, user.id)
    return UserMeRead(**UserRead.model_validate(user).model_dump(), permissions=codes)


async def list_users(session: AsyncSession, params: PageParams) -> dict[str, Any]:
    return await paginate(session, select(User).order_by(User.id.desc()), params)


async def create_user(session: AsyncSession, data: UserCreate) -> User:
    # 先查是为了给出明确的中文提示;并发竞态下唯一约束抛 IntegrityError,由全局 handler 转 409
    if await session.scalar(select(User).where(User.username == data.username)):
        raise AppError(ErrorCode.CONFLICT, "用户名已存在")
    if await session.scalar(select(User).where(User.email == data.email)):
        raise AppError(ErrorCode.CONFLICT, "邮箱已被注册")
    user = User(
        username=data.username,
        email=data.email,
        allow_multi_login=data.allow_multi_login,
        # scrypt 是 CPU 密集操作,放线程池避免阻塞事件循环
        hashed_password=await asyncio.to_thread(hash_password, data.password),
    )
    session.add(user)
    await session.commit()
    return user


async def update_user(
    session: AsyncSession, redis: Redis, actor_id: int, user_id: int, data: UserUpdate
) -> User:
    user = await get_user(session, user_id)
    changes = data.model_dump(exclude_none=True)
    if "email" in changes and changes["email"] != user.email:
        if await session.scalar(select(User).where(User.email == changes["email"])):
            raise AppError(ErrorCode.CONFLICT, "邮箱已被注册")
    disabling = changes.get("is_active") is False
    if disabling and user.id == actor_id:
        raise AppError(ErrorCode.INVALID_REQUEST, "不能停用自己")
    for field, value in changes.items():
        setattr(user, field, value)
    if disabling:
        await rbac_service.ensure_admin_remains(session)
    await session.commit()
    if disabling:
        await session_service.revoke_all(redis, str(user.id))
    return user


async def _set_password(session: AsyncSession, redis: Redis, user: User, new_password: str) -> None:
    user.hashed_password = await asyncio.to_thread(hash_password, new_password)
    # 先撤销再提交:撤销失败则回滚,不留下"密码已改、旧会话仍有效"
    await session_service.revoke_all(redis, str(user.id))
    await session.commit()


async def change_password(
    session: AsyncSession, redis: Redis, user: User, old_password: str, new_password: str
) -> None:
    # 依赖里已缓存了 user;加锁重读,避免用旧哈希验密
    user = await get_user(session, user.id, for_update=True)
    if not await asyncio.to_thread(verify_password, old_password, user.hashed_password):
        raise AppError(ErrorCode.INVALID_REQUEST, "原密码错误")
    await _set_password(session, redis, user, new_password)


async def reset_password(
    session: AsyncSession, redis: Redis, user_id: int, new_password: str
) -> None:
    user = await get_user(session, user_id, for_update=True)
    await _set_password(session, redis, user, new_password)
