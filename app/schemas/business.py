from typing import Literal

from pydantic import BaseModel


class BusinessResourceRead(BaseModel):
    key: str
    display_name: str
    required: bool


class BusinessEntryRead(BaseModel):
    kind: Literal["wms_callback", "ecs_event"]
    name: str
    device_role: str | None


class BusinessRead(BaseModel):
    code: str
    display_name: str
    device_roles: list[BusinessResourceRead]
    position_slots: list[BusinessResourceRead]
    initial_entries: list[BusinessEntryRead]
