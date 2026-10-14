from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token
from app.models import Permission, User, role_permission, user_role

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


def require_permission(perm: str):
    """路由依赖:当前用户的任一角色拥有 perm 才放行,否则 403。通过后返回当前用户。"""

    async def check(session: SessionDep, user: Annotated[User, Depends(get_current_user)]) -> User:
        # 每次请求一条查询;权限变更立即生效,不缓存
        stmt = (
            select(Permission.id)
            .join(role_permission, role_permission.c.permission_id == Permission.id)
            .join(user_role, user_role.c.role_id == role_permission.c.role_id)
            .where(user_role.c.user_id == user.id, Permission.code == perm)
            .limit(1)
        )
        if (await session.execute(stmt)).first() is None:
            raise AppError(ErrorCode.FORBIDDEN, "没有访问权限", detail=perm)
        return user

    return check
