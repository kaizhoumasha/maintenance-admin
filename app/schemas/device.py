from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

from app.integrations.ecs_status import StatusQueryResult, normalize_status_origin
from app.integrations.ecs_status.contracts import DeviceCode, EcsDeviceStatus
from app.schemas.base import NO_AUDIT, DatabaseId

Version = Annotated[int, Field(strict=True, ge=1)]


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceUpdate(DeviceInput):
    version: Version
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=500)
    sort_order: int = 0


class DeviceAssignment(DeviceInput):
    device_id: DatabaseId
    version: Version


class DeviceAssignmentsIn(DeviceInput):
    items: list[DeviceAssignment] = Field(min_length=1)
    work_line_id: DatabaseId | None = None

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({item.device_id for item in self.items}) != len(self.items):
            raise ValueError("device IDs must be unique")
        return self


class DeviceDiscoveryIn(DeviceInput):
    ecs_origin: str = Field(min_length=1, max_length=255)


class DeviceRegisterIn(DeviceInput):
    preview_token: str = Field(min_length=1, max_length=256, json_schema_extra=NO_AUDIT)
    device_codes: list[DeviceCode] = Field(min_length=1)
    work_line_id: DatabaseId | None = None

    @model_validator(mode="after")
    def unique_codes(self) -> Self:
        if len(set(self.device_codes)) != len(self.device_codes):
            raise ValueError("device codes must be unique")
        return self


class DeviceMigrationIn(DeviceInput):
    version: Version
    expected_origin: Annotated[str, AfterValidator(normalize_status_origin)] = Field(
        min_length=1, max_length=255
    )
    preview_token: str = Field(min_length=1, max_length=256, json_schema_extra=NO_AUDIT)
    reason: str = Field(min_length=1, max_length=500)
    confirmed: StrictBool

    @model_validator(mode="after")
    def explicit_confirmation(self) -> Self:
        if self.confirmed is not True or not self.reason.strip():
            raise ValueError("explicit confirmation and reason are required")
        return self


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: DatabaseId
    device_code: str
    display_name: str
    work_line_id: DatabaseId | None
    description: str | None
    sort_order: int
    current_route_id: str
    version: int
    created_at: datetime
    updated_at: datetime
    ecs_origin: str


class DevicePreviewRead(BaseModel):
    preview_token: str
    ecs_origin: str
    fetched_at: datetime
    expires_at: datetime
    items: list[dict]


class DeviceStatusRead(BaseModel):
    device_id: DatabaseId
    device_version: int
    current_route_id: str
    result: StatusQueryResult


class PreviewEntry(DeviceInput):
    classification: Literal["NEW", "EXISTING", "ADDRESS_CONFLICT"]
    device_id: int | None
    version: int | None
    route_id: str | None
    route_history_count: Annotated[int, Field(strict=True, ge=0)]
    data: EcsDeviceStatus


class PreviewSnapshot(DeviceInput):
    actor_id: Annotated[int, Field(strict=True, ge=1)]
    ecs_origin: str
    fetched_at: AwareDatetime
    expires_at: AwareDatetime
    entries: dict[str, PreviewEntry]
