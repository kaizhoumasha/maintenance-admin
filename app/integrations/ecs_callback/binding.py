"""Injected device binding context; no dependency on application services."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.strict_json import canonical_digest


@dataclass(frozen=True, kw_only=True)
class DeviceBinding:
    device_id: int
    device_code: str
    device_version: int
    work_line_id: int
    work_line_version: int
    device_role: str
    device_bindings: dict[str, str]
    position_bindings: dict[str, str]
    business_code: str | None = None

    @property
    def revision(self) -> str:
        return "2:" + canonical_digest(
            {
                "device_id": self.device_id,
                "device_version": self.device_version,
                "work_line_id": self.work_line_id,
                "work_line_version": self.work_line_version,
                "device_role": self.device_role,
            }
        )


BindingResolver = Callable[[AsyncSession, str], Awaitable[DeviceBinding | None]]
