"""Registry and handler context for ECS synchronous requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.registration import (
    RegistrationSource,
    registration_source,
    validate_function,
    validate_registration_file,
)
from app.integrations.wms_callback.operation import CallbackPayload

if TYPE_CHECKING:
    from app.integrations.ecs_callback.binding import DeviceBinding

_REQUEST_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENTRY_MODULES = {"ecs_request", "dev_ecs_request"}


@dataclass(frozen=True, slots=True)
class DeferredRequest:
    message_id: int
    operation: str
    operation_id: str


@dataclass
class EcsRequestContext:
    session: AsyncSession
    receipt_id: int
    digest: str
    device_code: str
    request_type: str
    timestamp: int
    binding: DeviceBinding | None
    response_model: type[BaseModel]
    deferred: DeferredRequest | None = None
    runtime: object | None = None
    business_code: str | None = None
    component_key: str | None = None
    _response: BaseModel | None = None

    @property
    def device_role(self) -> str | None:
        return self.binding.device_role if self.binding is not None else None

    @property
    def work_line_id(self) -> int | None:
        return self.binding.work_line_id if self.binding is not None else None

    @property
    def response(self) -> BaseModel | None:
        return self._response

    def respond(self, payload: BaseModel) -> None:
        if self._response is not None or self.deferred is not None:
            raise ValueError("request context already has a result")
        if not isinstance(payload, self.response_model):
            raise ValueError("response payload does not match the registered response model")
        self._response = payload

    def defer(self, message_id: int, operation: str, operation_id: str) -> None:
        candidate = DeferredRequest(message_id, operation, operation_id)
        if self._response is not None:
            raise ValueError("request context already has a result")
        if self.deferred is None:
            self.deferred = candidate
        elif self.deferred != candidate:
            raise ValueError("request context has a different deferred request")


@dataclass(frozen=True, slots=True)
class Registered:
    func: object
    data_model: type[CallbackPayload]
    response_model: type[BaseModel]
    registration_source: RegistrationSource


_registry: dict[tuple[str, str], Registered] = {}


def _validate_entry_module(target) -> None:
    module = target.__module__
    parts = module.split(".")
    if parts[:2] not in (["app", "businesses"], ["app", "integrations"]):
        return
    entry_index = 4 if parts[:3] == ["app", "businesses", "_components"] else 3
    if len(parts) <= entry_index or parts[entry_index] not in _ENTRY_MODULES:
        source = registration_source(target)
        raise ValueError(
            f"{source.location}: ecs_request registration must live under ecs_request or "
            "dev_ecs_request; 修复: 将入口移动到正确目录"
        )


def ecs_request(
    request_type: str,
    *,
    device_role: str,
    data: type[CallbackPayload],
    response: type[BaseModel],
):
    """Register async handler(ctx, data) for one ECS role/request type."""

    def register(func):
        if not isinstance(request_type, str) or not _REQUEST_TYPE.fullmatch(request_type):
            source = registration_source(func)
            raise ValueError(
                f"{source.location}: invalid request type {request_type!r}; "
                "修复: 使用大写字母开头的大写字母、数字或下划线"
            )
        source = validate_registration_file(request_type, func)
        _validate_entry_module(func)
        if not isinstance(device_role, str) or not _REQUEST_TYPE.fullmatch(device_role):
            raise ValueError(f"{source.location}: invalid device role {device_role!r}")
        if not (isinstance(response, type) and issubclass(response, BaseModel)):
            raise TypeError(f"{source.location}: response must be a Pydantic model")
        if response.model_config.get("extra") != "forbid":
            raise ValueError(
                f"{source.location}: response model must use extra='forbid'; "
                "修复: 为 response 设置 ConfigDict(extra='forbid')"
            )
        key = (device_role, request_type)
        if key in _registry:
            raise ValueError(
                f"duplicate ECS request binding {key!r}: "
                f"{_registry[key].registration_source.location} and {source.location}; "
                "修复: 每个(device_role, request_type)只注册一次"
            )
        validate_function(func, data)
        _registry[key] = Registered(func, data, response, source)
        return func

    return register


def lookup(device_role: str, request_type: str) -> Registered | None:
    return _registry.get((device_role, request_type))


def requests() -> dict[tuple[str, str], Registered]:
    return dict(_registry)


__all__ = [
    "DeferredRequest",
    "EcsRequestContext",
    "Registered",
    "ecs_request",
    "lookup",
    "requests",
]
