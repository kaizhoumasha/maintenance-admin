from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class DevSubmitOut(BaseModel):
    scenario: str
    title: str
    business_key: str
    client_request_id: str


class DevMessageOut(BaseModel):
    message_id: int
    target: str
    operation: str
    operation_id: str
    status: str
    fail_reason: str | None
    attempts: int
    not_received_count: int
    delivery_uncertain: bool
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    last_http_status: int | None
    last_failure_kind: str | None
    last_verdict: str | None
    request: Any
    response: Any
    last_response: Any
    created_at: datetime
    updated_at: datetime


class MockWmsMode(StrEnum):
    accept = "accept"  # 202 RECEIVED
    duplicate = "duplicate"  # 200 DUPLICATE
    conflict = "conflict"  # 409 CONFLICT
    reject = "reject"  # 422 REJECTED
    unavailable = "unavailable"  # 503 UNAVAILABLE,可重试
    garbage = "garbage"  # 200 非 JSON,判为 UNKNOWN
    hang = "hang"  # 不响应直到发送方超时,判为 UNKNOWN


class MockWmsModeBody(BaseModel):
    mode: MockWmsMode
