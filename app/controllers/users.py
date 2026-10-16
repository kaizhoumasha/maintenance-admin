from typing import Annotated, Any

from fastapi import Depends, Response, status

from app.core import (
    SessionDep,
    error_responses,
    get_current_user,
    require_permission,
)
from app.core.ratelimit import RateLimit
from app.core.redis import RedisDep
from app.core.routing import new_router
from app.core.security import REFRESH_COOKIE, REFRESH_COOKIE_PATH
from app.models import User
from app.schemas.base import DatabaseId, Page, PageQuery
from app.schemas.menu import RouteRecord
from app.schemas.rbac import UserRolesIn
from app.schemas.user import (
    PasswordChange,
    PasswordReset,
    UserCreate,
    UserMeRead,
    UserRead,
    UserUpdate,
)
from app.services import menu_service, rbac_service, user_service

router = new_router(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserMeRead)
async def read_me(
    current: Annotated[User, Depends(get_current_user)], session: SessionDep
) -> UserMeRead:
    return await user_service.get_me(session, current)


@router.get(
    "/me/menus",
    response_model=list[RouteRecord],
    response_model_exclude_none=True,
)
async def read_my_menus(
    current: Annotated[User, Depends(get_current_user)], session: SessionDep
) -> list[dict[str, Any]]:
    return await menu_service.get_user_routes(session, current.id)


# 旧密码可被盗用的 access token 暴力猜测,所以限流
@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(RateLimit(5))],
)
async def change_my_password(
    data: PasswordChange,
    response: Response,
    session: SessionDep,
    redis: RedisDep,
    current: Annotated[User, Depends(get_current_user)],
) -> None:
    await user_service.change_password(
        session, redis, current, data.old_password, data.new_password
    )
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


@router.get(
    "",
    response_model=Page[UserRead],
    dependencies=[Depends(require_permission("admin:user:list"))],
)
async def list_users(session: SessionDep, params: PageQuery) -> dict[str, Any]:
    return await user_service.list_users(session, params)


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(409),
    dependencies=[Depends(require_permission("admin:user:create"))],
)
async def create_user(data: UserCreate, session: SessionDep):
    return await user_service.create_user(session, data)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("admin:user:read"))],
)
async def get_user(user_id: DatabaseId, session: SessionDep):
    return await user_service.get_user(session, user_id)


@router.put(
    "/{user_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("admin:user:assign_role"))],
)
async def set_user_roles(user_id: DatabaseId, data: UserRolesIn, session: SessionDep) -> None:
    await rbac_service.set_user_roles(session, user_id, data.role_ids)


@router.put(
    "/{user_id}",
    response_model=UserRead,
    responses=error_responses(404, 409),
)
async def update_user(
    user_id: DatabaseId,
    data: UserUpdate,
    session: SessionDep,
    redis: RedisDep,
    actor: Annotated[User, Depends(require_permission("admin:user:update"))],
):
    return await user_service.update_user(session, redis, actor.id, user_id, data)


@router.post(
    "/{user_id}/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404),
    dependencies=[Depends(require_permission("admin:user:reset_password"))],
)
async def reset_password(
    user_id: DatabaseId, data: PasswordReset, session: SessionDep, redis: RedisDep
) -> None:
    await user_service.reset_password(session, redis, user_id, data.new_password)
