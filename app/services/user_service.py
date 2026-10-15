import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import AppError, ErrorCode, hash_password
from app.models import User
from app.schemas.user import UserCreate


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if not user:
        raise AppError(ErrorCode.NOT_FOUND, "用户不存在")
    return user


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
