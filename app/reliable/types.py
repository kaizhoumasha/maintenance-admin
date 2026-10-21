from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Protocol

from app.http_client import HttpRequest, HttpResult

if TYPE_CHECKING:
    from .config import TargetConfig


class MessageStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    STALLED = "STALLED"
    DONE = "DONE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class FailReason(StrEnum):
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    FAILURE = "FAILURE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
    LOCAL_ERROR = "LOCAL_ERROR"


class VerdictKind(StrEnum):
    DELIVERED = "DELIVERED"
    NOT_RECEIVED = "NOT_RECEIVED"
    PEER_TERMINAL = "PEER_TERMINAL"
    UNKNOWN = "UNKNOWN"


class InvalidMessage(ValueError):
    pass


class DuplicateMessage(ValueError):
    pass


@dataclass(frozen=True)
class MessageDraft:
    target: str
    operation: str
    operation_id: str
    request_raw: bytes
    ttl: timedelta | None = None
    business_key: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    target: str
    operation: str
    operation_id: str
    request_raw: bytes
    business_key: str | None
    trace_id: str | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class EnqueueResult:
    message_id: int
    status: MessageStatus


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    response: bytes | None = None
    retry_after_ms: float | None = None
    reason: FailReason | None = None

    def __post_init__(self):
        if not isinstance(self.kind, VerdictKind):
            raise ValueError("invalid verdict kind")
        if self.kind == VerdictKind.DELIVERED and not isinstance(self.response, bytes):
            raise ValueError("delivered response must be bytes")
        if self.response is not None and not isinstance(self.response, bytes):
            raise ValueError("response must be bytes")
        if self.retry_after_ms is not None:
            if (
                self.kind != VerdictKind.NOT_RECEIVED
                or isinstance(self.retry_after_ms, bool)
                or not isinstance(self.retry_after_ms, (int, float))
                or not isfinite(self.retry_after_ms)
                or self.retry_after_ms < 0
            ):
                raise ValueError("invalid retry hint")
        if self.kind == VerdictKind.PEER_TERMINAL:
            if not isinstance(self.reason, FailReason) or self.reason == FailReason.LOCAL_ERROR:
                raise ValueError("invalid peer terminal reason")
        elif self.reason is not None:
            raise ValueError("unexpected terminal reason")


class ProtocolAdapter(Protocol):
    def build_request(self, message: OutboundMessage, target: "TargetConfig") -> HttpRequest: ...

    def classify_response(self, message: OutboundMessage, result: HttpResult) -> Verdict: ...
