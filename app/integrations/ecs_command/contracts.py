from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_RETRY_WINDOW = timedelta(seconds=300)

Millis = Annotated[int, Field(ge=0, le=2**63 - 1)]
Text = Annotated[str, Field(min_length=1, pattern=r"^[^\x00]+$")]


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    device_code: Text
    command_code: Text
    task_type: Text
    priority: Annotated[int, Field(ge=1, le=1)]
    timeout: Millis
    timestamp: Millis
    params: dict


class ErrorDetail(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    code: str
    msg: str


class ResultEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    command_code: Text
    device_code: Text
    result: Literal["SUCCESS", "FAILED"]
    finish_time: Millis
    data: dict = {}
    error_detail: ErrorDetail | None = None

    @model_validator(mode="after")
    def check_error(self):
        if (self.result == "FAILED") != (self.error_detail is not None):
            raise ValueError("FAILED requires error_detail; SUCCESS forbids it")
        return self


@dataclass(frozen=True)
class CommandView:
    command_code: str
    status: str
    delivery_status: str
    ack_received: bool
    result: dict | None
    hold_reason: str | None
    needs_reconciliation: bool


@dataclass(frozen=True)
class DeviceAnnex:
    params_model: type[BaseModel]
    result_model: type[BaseModel]
    error_model: type[ErrorDetail]
    timeout_min_ms: int
    timeout_max_ms: int

    def __post_init__(self):
        if (
            type(self.timeout_min_ms) is not int
            or type(self.timeout_max_ms) is not int
            or not (0 <= self.timeout_min_ms <= self.timeout_max_ms <= 2**63 - 1)
        ):
            raise ValueError("annex requires a confirmed timeout range")


class PauseReason(StrEnum):
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    LEASE_INTERRUPTED = "LEASE_INTERRUPTED"
    CONTRACT_NON_COMPLIANT = "CONTRACT_NON_COMPLIANT"
    CONFIG_DRIFT = "CONFIG_DRIFT"
    AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"
    PRE_GATE_VALIDATION = "PRE_GATE_VALIDATION"
    ASSEMBLY_UNAVAILABLE = "ASSEMBLY_UNAVAILABLE"
    EARLY_RESULT_LOCK = "EARLY_RESULT_LOCK"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
