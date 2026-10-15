import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.core import get_current_user, get_settings
from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode, ErrorResponse
from app.core.ratelimit import RateLimit
from app.core.redis import RedisDep
from app.core.security import decode_token, verify_password
from app.models import User
from app.schemas.auth import LoginIn, TokenOut
from app.services import session_service

router = APIRouter(prefix="/auth", tags=["auth"])

# 用户不存在时也跑一次哈希，避免用时差暴露"这个用户名是否存在"
_DUMMY_HASH = "$".join(["0" * 32, "0" * 64])

REFRESH_COOKIE = "refresh_token"
# 与 main.py 的 /api/v1 前缀一致;cookie 只会被发往认证接口
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _token_out(response: Response, access: str, refresh: str) -> TokenOut:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        httponly=True,
        secure=settings.ENVIRONMENT != "local",
        samesite="strict",  # 前后端须同站(同域或 dev 代理);跨站部署需改 None 并补 CSRF 防护
        path=REFRESH_COOKIE_PATH,
    )
    return TokenOut(access_token=access)


def _refresh_payload(request: Request) -> dict:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AppError(ErrorCode.UNAUTHORIZED, "未提供凭证")
    return decode_token(token, "refresh")


# 防暴力破解:同一 IP 每分钟 5 次(_DUMMY_HASH 只防枚举)
@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[Depends(RateLimit(5, name="login", per_route=False))],
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def login(
    data: LoginIn, session: SessionDep, redis: RedisDep, response: Response
) -> TokenOut:
    user = await session.scalar(select(User).where(User.username == data.username))
    if user is None:
        await asyncio.to_thread(verify_password, data.password, _DUMMY_HASH)
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    valid = await asyncio.to_thread(verify_password, data.password, user.hashed_password)
    if not valid or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    return _token_out(response, *await session_service.issue_tokens(redis, user))


@router.post("/refresh", response_model=TokenOut, responses={401: {"model": ErrorResponse}})
async def refresh(
    request: Request, response: Response, session: SessionDep, redis: RedisDep
) -> TokenOut:
    payload = _refresh_payload(request)
    # 先校验用户再消费旧 jti:停用用户不会白白烧掉 token;sub 来自已验签的 payload
    user = await session.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    await session_service.claim_refresh(redis, payload)
    # shortcut: claim 之后 Redis 写入失败(503)会丢这一个会话,用户需重新登录
    return _token_out(response, *await session_service.issue_tokens(redis, user))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"model": ErrorResponse}},
)
async def logout(
    request: Request,
    response: Response,
    redis: RedisDep,
    current: Annotated[User, Depends(get_current_user)],
) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    if REFRESH_COOKIE not in request.cookies:
        return
    try:
        payload = _refresh_payload(request)
    except AppError:  # 过期/无效的 cookie 没有可撤销的会话,只清 cookie
        return
    if payload["sub"] != str(current.id):  # 不能撤销别人的 token
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    # access token 无状态、不进黑名单,最长 ACCESS_TOKEN_EXPIRE_MINUTES 内仍可用
    await session_service.revoke_refresh(redis, payload)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"model": ErrorResponse}},
)
async def logout_all(
    response: Response, redis: RedisDep, current: Annotated[User, Depends(get_current_user)]
) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    await session_service.revoke_all(redis, str(current.id))
