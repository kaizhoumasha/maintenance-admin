from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token
from app.models import User

# auto_error=False: 缺少凭证时由下面抛 AppError,保持统一的错误响应格式
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if creds is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "未提供凭证")
    payload = decode_token(creds.credentials)
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError) as e:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效") from e

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "凭证无效")
    return user
