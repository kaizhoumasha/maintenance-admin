from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import PERMISSION_CODE_PATTERN


class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str


class PermissionCreate(BaseModel):
    code: str = Field(max_length=64, pattern=PERMISSION_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=64)


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    permissions: list[PermissionRead]


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permissions: list[str] = []


class RolePermissionsIn(BaseModel):
    permissions: list[str]


class UserRolesIn(BaseModel):
    role_ids: list[int]
