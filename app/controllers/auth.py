import asyncio

from fastapi import APIRouter
from sqlalchemy import select

from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode, ErrorResponse
from app.core.security import create_access_token, verify_password
from app.models import User
from app.schemas.auth import LoginIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])

# 用户不存在时也跑一次哈希，避免用时差暴露"这个用户名是否存在"
_DUMMY_HASH = "$".join(["0" * 32, "0" * 64])


@router.post("/login", response_model=TokenOut, responses={401: {"model": ErrorResponse}})
async def login(data: LoginIn, session: SessionDep) -> TokenOut:
    user = await session.scalar(select(User).where(User.username == data.username))
    if user is None:
        await asyncio.to_thread(verify_password, data.password, _DUMMY_HASH)
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    valid = await asyncio.to_thread(verify_password, data.password, user.hashed_password)
    if not valid or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    return TokenOut(access_token=create_access_token(str(user.id)))
