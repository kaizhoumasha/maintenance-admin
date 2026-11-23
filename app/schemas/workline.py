from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from app.schemas.base import DatabaseId

BusinessCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]

BindingName = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=False, pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
]
BindingCode = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=100),
]


Capacity = Annotated[int, Field(strict=True, ge=1, le=2147483647)]


class WorkLinePositionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_code: BindingCode
    capacity: Capacity | None = 1

    @field_validator("position_code")
    @classmethod
    def check_code(cls, value):
        if value != value.strip() or "\x00" in value:
            raise ValueError("位置编码不能含NUL或首尾空白")
        return value


class WorkLinePositionRead(WorkLinePositionWrite):
    model_config = ConfigDict(from_attributes=True)
    id: DatabaseId


class WorkLineEditable(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    line_name: str = Field(min_length=1, max_length=100)
    is_enabled: Annotated[bool, Field(strict=True)] = True
    positions: list[WorkLinePositionWrite] = Field(default_factory=list, strict=True)
    zone_name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    sort_order: int = 0
    device_bindings: dict[BindingName, BindingCode] = Field(default_factory=dict, strict=True)
    position_bindings: dict[BindingName, BindingCode] = Field(default_factory=dict, strict=True)

    @field_validator("device_bindings", "position_bindings")
    @classmethod
    def check_bindings(cls, value, info: ValidationInfo):
        if any(code != code.strip() or "\x00" in code for code in value.values()):
            raise ValueError("绑定编码不能为空、含NUL或首尾空白")
        if info.field_name == "device_bindings" and len(set(value.values())) != len(value):
            raise ValueError("同一编码只能绑定一个角色")
        return value


class WorkLineCreate(WorkLineEditable):
    business_code: BusinessCode
    line_code: str = Field(min_length=1, max_length=50)


class WorkLineUpdate(WorkLineEditable):
    business_code: BusinessCode | None = None
    version: Annotated[int, Field(strict=True, ge=1)]


class WorkLineRead(WorkLineCreate):
    model_config = ConfigDict(from_attributes=True)

    business_code: BusinessCode | None
    id: DatabaseId
    version: int
    created_at: datetime
    updated_at: datetime
    positions: list[WorkLinePositionRead] = Field(default_factory=list)
    device_count: int = 0
