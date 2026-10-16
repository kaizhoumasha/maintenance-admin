import re
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.database import SessionDep
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_token
from app.models import Permission, User, role_permission, user_role

# 权限码三段式 <module>:<resource>:<action>,如 admin:user:create;小写字母/数字/下划线
PERMISSION_CODE_PATTERN = r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$"
# 一个权限码只能对应一次 require_permission(即一个路由),在导入时就拦住重复
registered_codes: set[str] = set()

# auto_error=False: 缺少凭证时由下面抛 AppError,保持统一的错误响应格式
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
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
    request.state.audit_user_id = user.id  # 供审计中间件读取
    return user


def require_permission(perm: str):
    """路由依赖:当前用户的任一角色拥有 perm 才放行,否则 403。通过后返回当前用户。

    perm 必须是三段式且全局只能用一次,否则导入时抛 ValueError。
    """
    if not re.fullmatch(PERMISSION_CODE_PATTERN, perm):
        raise ValueError(f"权限码格式错误,应为 <module>:<resource>:<action>: {perm!r}")
    if perm in registered_codes:
        raise ValueError(f"权限码重复使用: {perm!r}(一个权限码只能对应一个路由)")
    registered_codes.add(perm)

    async def check(
        request: Request, session: SessionDep, user: Annotated[User, Depends(get_current_user)]
    ) -> User:
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
        request.state.audit_action = perm  # 通过后才写,403 因此不会被审计
        return user

    check.requires_permission = perm  # type: ignore[attr-defined]  # 供 DocumentedRoute 识别
    return check
