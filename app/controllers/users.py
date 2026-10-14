from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core import ErrorResponse, SessionDep, get_current_user, require_permission
from app.models import User
from app.schemas.rbac import UserRolesIn
from app.schemas.user import UserCreate, UserRead
from app.services import rbac_service, user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead, responses={401: {"model": ErrorResponse}})
async def read_me(current: Annotated[User, Depends(get_current_user)]) -> User:
    return current


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("user:create"))],
)
async def create_user(data: UserCreate, session: SessionDep):
    return await user_service.create_user(session, data)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("user:read"))],
)
async def get_user(user_id: int, session: SessionDep):
    return await user_service.get_user(session, user_id)


@router.put(
    "/{user_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
    dependencies=[Depends(require_permission("role:assign"))],
)
async def set_user_roles(user_id: int, data: UserRolesIn, session: SessionDep) -> None:
    await rbac_service.set_user_roles(session, user_id, data.role_ids)
