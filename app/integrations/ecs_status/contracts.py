"""固定 Status 合同与查询来源；不承载登记、运行许可或同步策略。"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CAPABILITY_NAME = "ecs.device.status@v1"


def _device_code(value: str) -> str:
    if value != value.strip() or "\x00" in value:
        raise ValueError("device_code must preserve a nonblank wire identity")
    return value


DeviceCode = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_device_code)]
CommandName = Annotated[str, Field(min_length=1)]


class StatusModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


class EcsDeviceInfo(StatusModel):
    device_code: DeviceCode
    device_name: Annotated[str, Field(min_length=1, max_length=200)] | None
    device_type: Annotated[str, Field(min_length=1, max_length=100)] | None
    role: Annotated[str, Field(min_length=1, max_length=100)] | None
    supported_commands: tuple[CommandName, ...] | None
    supported_events: tuple[CommandName, ...] | None


class EcsDeviceState(StatusModel):
    device_code: DeviceCode
    mode: Literal["AUTO", "MANUAL", "MAINTENANCE", "UNKNOWN"]
    status: Literal["IDLE", "RUNNING", "ERROR", "PAUSED", "STOPPED", "OFFLINE", "UNKNOWN"]
    is_online: bool
    current_command_code: Annotated[str, Field(min_length=1, max_length=160)] | None
    scenario: Annotated[str, Field(min_length=1, max_length=100)] | None
    updated_at: Annotated[int, Field(gt=0, le=2**63 - 1)]


class EcsDeviceStatus(StatusModel):
    device: EcsDeviceInfo
    state: EcsDeviceState

    @model_validator(mode="after")
    def same_identity(self) -> Self:
        if self.device.device_code != self.state.device_code:
            raise ValueError("device and state identities must match")
        return self


class EcsStatusResponse(StatusModel):
    devices: tuple[EcsDeviceStatus, ...]

    @model_validator(mode="after")
    def unique_identities(self) -> Self:
        codes = [item.device.device_code for item in self.devices]
        if len(codes) != len(set(codes)):
            raise ValueError("device identities must be unique within one ECS response")
        return self


class StatusQueryIn(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    ecs_origin: Annotated[
        str,
        Field(
            min_length=1,
            max_length=255,
            description="HTTP(S) IP origin，不含路径；联调时填本地 Mock 地址",
            examples=["http://127.0.0.1:8001"],
        ),
    ]
    device_code: DeviceCode | None = Field(None, description="不传则查询该 ECS 下全部设备")


class StatusQueryResult(StatusModel):
    capability_name: Literal["ecs.device.status@v1"] = CAPABILITY_NAME
    ecs_origin: str
    requested_device_code: str | None
    fetched_at: AwareDatetime
    devices: tuple[EcsDeviceStatus, ...]


class StatusQueryError(Exception):
    """Only allowlisted classification and request provenance, never raw peer data."""

    capability_name = CAPABILITY_NAME

    def __init__(
        self,
        *,
        ecs_origin: str,
        requested_device_code: str | None,
        completed_at: datetime,
        error_kind: str,
        http_status: int | None = None,
        retry_after_at: datetime | None = None,
    ):
        super().__init__(error_kind)
        self.ecs_origin = ecs_origin
        self.requested_device_code = requested_device_code
        self.completed_at = completed_at
        self.error_kind = error_kind
        self.http_status = http_status
        self.retry_after_at = retry_after_at
