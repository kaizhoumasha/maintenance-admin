"""Local delivery observations and persisted state; neither claims physical completion."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class DevEcsEventDelivery(BaseModel):
    digest: str | None
    status_code: int
    outcome: str
    response: dict | None
    diagnostic: list[dict] | None


class DevEcsEventReceipt(BaseModel):
    device_code: str | None
    status_code: int
    response: dict
    diagnostic: list[dict] | None
    created_at: datetime


class DevEcsEventState(BaseModel):
    digest: str
    receipt: DevEcsEventReceipt | None
    business: dict[str, Any] | None
