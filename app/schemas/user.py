from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import NO_AUDIT

Email = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)]
NewPassword = Annotated[str, Field(min_length=8, max_length=128, json_schema_extra=NO_AUDIT)]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: Email
    password: NewPassword
    allow_multi_login: bool = False


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    is_active: bool
    allow_multi_login: bool
    created_at: datetime


class UserMeRead(UserRead):
    permissions: list[str]


class UserUpdate(BaseModel):
    """未传(或为 null)的字段保持不变。"""

    email: Email | None = None
    is_active: bool | None = None
    allow_multi_login: bool | None = None


class PasswordChange(BaseModel):
    old_password: str = Field(max_length=128, json_schema_extra=NO_AUDIT)
    new_password: NewPassword


class PasswordReset(BaseModel):
    new_password: NewPassword
