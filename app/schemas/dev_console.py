from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

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


class DevOperationOut(BaseModel):
    name: str
    endpoint: str
    title: str
    ttl_seconds: float | None
    payload_schema: dict[str, Any]
    example: dict[str, Any] | None


class DevRegistrationOut(BaseModel):
    kind: Literal["ecs_event", "wms_callback", "wms_request"]
    name: str
    module: str
    source_file: str
    symbol: str
    local_only: bool
    endpoint: str | None
    payload_schema: dict[str, Any] | None


class DevRequestOut(BaseModel):
    operation: str
    operation_id: str
    message_id: int
    status: str
    target: str


class MockWmsMode(StrEnum):
    accept = "accept"  # 202 RECEIVED
    duplicate = "duplicate"  # 200 DUPLICATE
    conflict = "conflict"  # 409 CONFLICT
    reject = "reject"  # 422 REJECTED
    unavailable = "unavailable"  # 503 UNAVAILABLE,可重试
    garbage = "garbage"  # 200 非 JSON,判为 UNKNOWN
    hang = "hang"  # 不响应直到发送方超时,判为 UNKNOWN
    busy = "busy"  # 429 BUSY 带 retry_after_ms;WMS 请求端点按合同重试,Transport 提交判为 UNKNOWN
    prepare_accepted = "prepare_accepted"  # 仅 decisions:202 PREPARE_ACCEPTED,其余端点同 accept


class MockWmsModeBody(BaseModel):
    mode: MockWmsMode
