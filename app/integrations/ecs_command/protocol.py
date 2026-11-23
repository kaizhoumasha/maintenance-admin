import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.core.strict_json import canonical_digest, json_object
from app.http_client import HttpRequest
from app.reliable.types import FailReason, Verdict, VerdictKind

PROTOCOL = "ecs-command"
OPERATION = "ecs.command"


def retry_after(headers: tuple[tuple[str, str], ...], now: datetime) -> int | float | None:
    values = [v.strip() for k, v in headers if k.lower() == "retry-after"]
    if len(values) != 1:
        return None
    value = values[0]
    try:
        if re.fullmatch(r"[0-9]+", value):
            seconds = int(value)
            return seconds if seconds <= 2**31 - 1 else None
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        seconds = (date - now).total_seconds()
        return seconds if 0 < seconds <= 2**31 - 1 else None
    except (ValueError, TypeError, OverflowError):
        return None


class EcsCommandAdapter:
    def __init__(self, *, clock=lambda: datetime.now(UTC)):
        self.clock = clock

    def build_request(self, message, target):
        return HttpRequest(
            "POST",
            "/api/v1/device/command",
            headers=(("Content-Type", "application/json"),),
            body=message.request_raw,
        )

    def classify_response(self, message, result):
        unknown = Verdict(VerdictKind.UNKNOWN)
        content = [
            v.split(";", 1)[0].strip().lower()
            for k, v in result.headers
            if k.lower() == "content-type"
        ]
        body = json_object(result.body) if isinstance(result.body, bytes) else None
        if content != ["application/json"] or body is None:
            return unknown
        try:
            canonical_digest(body)
        except (ValueError, RecursionError):
            return unknown
        code, msg = body.get("code"), body.get("message")
        if type(code) is not int or code != result.status_code or not isinstance(msg, str):
            return unknown
        if "trace_id" in body and not isinstance(body["trace_id"], str):
            return unknown
        if code == 200 and msg == "ACK":
            return Verdict(VerdictKind.DELIVERED, response=result.body)
        if (code, msg) in {
            (400, "INVALID_ENVELOPE"),
            (404, "DEVICE_NOT_FOUND"),
            (405, "METHOD_NOT_ALLOWED"),
            (413, "PAYLOAD_TOO_LARGE"),
            (422, "ANNEX_VALIDATION_FAILED"),
        }:
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
        if code == 409 and msg == "IDEMPOTENCY_CONFLICT":
            return Verdict(VerdictKind.HOLD, pause_reason="IDENTITY_CONFLICT")
        if code == 503 and msg == "TEMPORARILY_UNAVAILABLE":
            return Verdict(VerdictKind.NOT_RECEIVED)
        if code == 429 and msg == "CAPACITY_EXCEEDED":
            delay = retry_after(result.headers, self.clock())
            if delay is None:
                return Verdict(VerdictKind.HOLD, pause_reason="CONTRACT_NON_COMPLIANT")
            return Verdict(VerdictKind.NOT_RECEIVED, retry_after_ms=delay * 1000)
        return unknown
