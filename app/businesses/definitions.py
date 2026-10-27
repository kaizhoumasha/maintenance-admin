"""Immutable business resource declarations, independent of runtime dependencies."""

import re
from dataclasses import dataclass
from typing import Literal

_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,99}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,99}")
_EVENT = re.compile(r"[A-Z][A-Z0-9_]*")


def _text(value: str, field: str, max_length: int = 100) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field} must be a nonempty string of at most {max_length} characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceRole:
    role_key: str
    display_name: str
    required: bool = True

    def __post_init__(self):
        if not isinstance(self.role_key, str) or not _KEY.fullmatch(self.role_key):
            raise ValueError(f"invalid device role key: {self.role_key!r}")
        _text(self.display_name, "display_name")
        if type(self.required) is not bool:
            raise ValueError("required must be bool")


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionSlot:
    slot_key: str
    display_name: str
    required: bool = True

    def __post_init__(self):
        if not isinstance(self.slot_key, str) or not _KEY.fullmatch(self.slot_key):
            raise ValueError(f"invalid position slot key: {self.slot_key!r}")
        _text(self.display_name, "display_name")
        if type(self.required) is not bool:
            raise ValueError("required must be bool")


@dataclass(frozen=True, slots=True, kw_only=True)
class InitialEntry:
    kind: Literal["wms_callback", "ecs_event"]
    name: str
    device_role: str | None = None

    def __post_init__(self):
        _text(self.name, "entry name", 256)
        if self.kind == "ecs_event":
            if not _EVENT.fullmatch(self.name):
                raise ValueError(f"invalid ECS event name: {self.name!r}")
            if not isinstance(self.device_role, str) or not _KEY.fullmatch(self.device_role):
                raise ValueError(f"invalid ECS entry device role: {self.device_role!r}")
        elif self.kind == "wms_callback":
            if self.device_role is not None:
                raise ValueError("WMS callback entry cannot specify a device role")
        else:
            raise ValueError(f"invalid initial entry kind: {self.kind!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class BusinessDefinition:
    code: str
    display_name: str
    device_roles: tuple[DeviceRole, ...] = ()
    position_slots: tuple[PositionSlot, ...] = ()
    initial_entries: tuple[InitialEntry, ...] = ()

    def __post_init__(self):
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise ValueError(f"invalid business code: {self.code!r}")
        _text(self.display_name, "display_name")
        for field, item_type, key_field in (
            ("device_roles", DeviceRole, "role_key"),
            ("position_slots", PositionSlot, "slot_key"),
            ("initial_entries", InitialEntry, None),
        ):
            values = getattr(self, field)
            if not isinstance(values, tuple) or any(not isinstance(v, item_type) for v in values):
                raise ValueError(f"{self.code}: {field} must be a tuple of {item_type.__name__}")
            keys = [getattr(v, key_field) if key_field else v for v in values]
            if len(set(keys)) != len(keys):
                raise ValueError(f"{self.code}: duplicate {field}")
        role_keys = {role.role_key for role in self.device_roles}
        for entry in self.initial_entries:
            if entry.kind == "ecs_event" and entry.device_role not in role_keys:
                raise ValueError(
                    f"{self.code}: undeclared initial entry role {entry.device_role!r}"
                )
