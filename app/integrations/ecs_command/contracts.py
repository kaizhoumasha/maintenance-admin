import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.registration import RegistrationSource, validate_registration_source


@dataclass(frozen=True)
class CommandBinding:
    device_role: str
    task_type: str
    annex_id: str
    annex_version: str
    source: object
    registration_source: RegistrationSource = field(init=False)

    def __post_init__(self):
        source = validate_registration_source(self.source)
        for name in (self.device_role, self.task_type):
            if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                raise ValueError(f"{source.location}: invalid ECS role/task {name!r}")
        if any(
            not isinstance(v, str) or not v.strip() for v in (self.annex_id, self.annex_version)
        ):
            raise ValueError(f"{source.location}: explicit annex identity/version required")
        object.__setattr__(self, "registration_source", source)


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


def merge_annexes(existing, declared, *, source):
    """Merge original contracts without changing the meaning of a persisted annex key."""
    result = dict(existing)
    for key, annex in declared.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or any(not isinstance(v, str) or not v.strip() or "\x00" in v for v in key)
            or not isinstance(annex, DeviceAnnex)
        ):
            raise ValueError(f"{source}: invalid DeviceAnnex {key!r}")
        previous = result.get(key)
        if previous is not None and (
            previous.params_model is not annex.params_model
            or previous.result_model is not annex.result_model
            or previous.error_model is not annex.error_model
            or previous.timeout_min_ms != annex.timeout_min_ms
            or previous.timeout_max_ms != annex.timeout_max_ms
        ):
            raise ValueError(f"{source}: conflicting annex {key!r}")
        result[key] = annex
    return result


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
