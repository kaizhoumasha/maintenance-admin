from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core import ErrorResponse, SessionDep, get_current_user
from app.models import User
from app.schemas.user import UserCreate, UserRead
from app.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead, responses={401: {"model": ErrorResponse}})
async def read_me(current: Annotated[User, Depends(get_current_user)]) -> User:
    return current


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
)
async def create_user(data: UserCreate, session: SessionDep):
    return await user_service.create_user(session, data)


@router.get("/{user_id}", response_model=UserRead, responses={404: {"model": ErrorResponse}})
async def get_user(user_id: int, session: SessionDep):
    return await user_service.get_user(session, user_id)
