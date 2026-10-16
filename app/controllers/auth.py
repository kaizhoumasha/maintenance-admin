from typing import Annotated

from fastapi import Depends, Request, Response, status

from app.core import get_current_user, get_settings
from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode, error_responses
from app.core.ratelimit import RateLimit
from app.core.redis import RedisDep
from app.core.routing import new_router
from app.core.security import REFRESH_COOKIE, REFRESH_COOKIE_PATH, decode_token
from app.models import User
from app.schemas.auth import LoginIn, TokenOut
from app.services import session_service, user_service

router = new_router(prefix="/auth", tags=["auth"])


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


# 防暴力破解:同一 IP 每分钟 5 次
@router.post(
    "/login",
    response_model=TokenOut,
    dependencies=[Depends(RateLimit(5, name="login", per_route=False))],
    responses=error_responses(401),
)
async def login(
    data: LoginIn, session: SessionDep, redis: RedisDep, response: Response
) -> TokenOut:
    user = await user_service.authenticate(session, data.username, data.password)
    return _token_out(response, *await session_service.issue_tokens(redis, user))


@router.post("/refresh", response_model=TokenOut, responses=error_responses(401))
async def refresh(
    request: Request, response: Response, session: SessionDep, redis: RedisDep
) -> TokenOut:
    payload = _refresh_payload(request)
    # 先校验用户再消费旧 jti:停用用户不会白白烧掉 token;sub 来自已验签的 payload
    user = await user_service.lock_user(session, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    return _token_out(response, *await session_service.issue_tokens(redis, user, payload["jti"]))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
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
)
async def logout_all(
    response: Response, redis: RedisDep, current: Annotated[User, Depends(get_current_user)]
) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    await session_service.revoke_all(redis, str(current.id))
