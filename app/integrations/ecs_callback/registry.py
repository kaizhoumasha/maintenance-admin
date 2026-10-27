"""ECS 事件处理器注册表:(逻辑角色,事件类型) → async handler(ctx, data)。

与 WMS 回调同样的注册做法:重复注册报错并指出两个函数;注册表是模块级全局状态,
测试用 `monkeypatch` 替换成空 dict 隔离。处理器是普通函数,不返回 Outcome。
"""

import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.registration import (
    RegistrationSource,
    registration_source,
    validate_function,
    validate_registration_file,
)
from app.integrations.wms_callback.operation import CallbackPayload

from .binding import DeviceBinding

_EVENT_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, kw_only=True)
class EcsEventContext:
    session: AsyncSession
    receipt_id: int
    digest: str
    device_code: str
    event_type: str
    timestamp: int
    binding: DeviceBinding
    notes: list[dict] = field(default_factory=list)
    runtime: object | None = None

    @property
    def device_role(self) -> str:
        return self.binding.device_role

    @property
    def work_line_id(self) -> int:
        return self.binding.work_line_id

    def note(self, reason_code: str, **detail) -> None:
        """附上诊断:事件已接收但没有(完全)应用的事实,随收据同事务持久化。"""
        self.notes.append({"reason_code": reason_code, **detail})


@dataclass(frozen=True)
class Registered:
    func: object
    data_model: type[CallbackPayload] | None
    registration_source: RegistrationSource


_registry: dict[tuple[str, str], Registered] = {}


def ecs_event(event_type: str, *, device_role: str, data: type[CallbackPayload]):
    """注册 `async def handler(ctx: EcsEventContext, data) -> None`;返回原函数。"""

    def register(func):
        if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
            source = registration_source(func)
            raise ValueError(
                f"{source.location}: invalid event type {event_type!r}; "
                "修复: 使用大写字母开头的大写字母、数字或下划线，例如 SCAN_COMPLETED"
            )
        source = validate_registration_file(event_type, func)
        if not isinstance(device_role, str) or not _EVENT_TYPE.fullmatch(device_role):
            raise ValueError(f"{source.location}: invalid device role {device_role!r}")
        key = (device_role, event_type)
        if key in _registry:
            raise ValueError(
                f"duplicate event binding {key!r}: "
                f"{_registry[key].registration_source.location} and {source.location}; "
                "修复: 每个(device_role, event_type)只注册一次"
            )
        validate_function(func, data)
        _registry[key] = Registered(func, data, source)
        return func

    return register


def lookup(device_role: str, event_type: str) -> Registered | None:
    return _registry.get((device_role, event_type))


def events() -> dict[tuple[str, str], Registered]:
    return dict(_registry)
