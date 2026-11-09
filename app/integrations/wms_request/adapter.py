"""Classify common decisions and facts envelopes and declared decision data."""

import re
from hmac import digest

from pydantic import ValidationError

from app.core import get_settings
from app.core.strict_json import canonical_digest, json_object
from app.http_client import HttpRequest, HttpResult
from app.integrations.wms_envelope import UNKNOWN, read_ack
from app.reliable import FailReason, OutgoingMessage, Verdict, VerdictKind
from app.reliable.config import TargetConfig

from .operation import Endpoint, lookup

MOCK_DECISIONS_TARGET = "xray-mock-wms-decisions"
MOCK_SIGNATURE_HEADER = "x-wes-mock-signature"


def mock_signature(body: bytes) -> bytes:
    secret = get_settings().JWT_SECRET_KEY.encode()
    return digest(secret, b"xray-wms-mock\0" + body, "sha256").hex().encode()


CONFLICT_REASONS = frozenset(
    {
        "IDEMPOTENCY_CONFLICT",
        "REVISION_CONFLICT",
        "STATE_CONFLICT",
        "REFERENCE_CONFLICT",
        "POSITION_CONFLICT",
    }
)
REJECTION_REASONS = frozenset({"INVALID_ENVELOPE", "UNSUPPORTED_OPERATION", "INVALID_DATA"})


class WmsEndpointAdapter:
    """同步成功响应由端点决定:facts 为 200 RECORDED / DUPLICATE,decisions 为 200 DECIDED /
    202 PREPARE_ACCEPTED。同步决定可声明 data 合同，业务应用仍归对应 operation。"""

    def __init__(self, endpoint: Endpoint, path: str, success: frozenset[tuple[int, str]]):
        self.endpoint, self.path, self.success = endpoint, path, success

    def build_request(self, message: OutgoingMessage, target: TargetConfig) -> HttpRequest:
        request = json_object(message.request_raw) or {}
        registered = lookup(message.operation)
        if (
            request.get("operation") != message.operation
            or request.get("operation_id") != message.operation_id
            or registered is None
            or registered.endpoint != self.endpoint
        ):
            raise ValueError("frozen request does not match this endpoint")
        headers = (("accept", "application/json"), ("content-type", "application/json"))
        if target.explicit_only and target.name == MOCK_DECISIONS_TARGET:
            headers += ((MOCK_SIGNATURE_HEADER, mock_signature(message.request_raw).decode()),)
        return HttpRequest(
            method="POST",
            path=self.path,
            headers=headers,
            body=message.request_raw,
        )

    def same_request(self, existing_raw: bytes, new_raw: bytes) -> bool:
        """信封里每次入队都会换 timestamp,所以只比较身份、operation 与 data(区分 1 与 1.0)。"""
        old, new = json_object(existing_raw), json_object(new_raw)
        if old is None or new is None:
            return False
        try:
            return (
                old.get("operation_id") == new.get("operation_id")
                and old.get("operation") == new.get("operation")
                and canonical_digest(old.get("data")) == canonical_digest(new.get("data"))
            )
        except (ValueError, RecursionError):
            return False

    def classify_response(self, message: OutgoingMessage, result: HttpResult) -> Verdict:
        ack = read_ack(message, result)
        if isinstance(ack, Verdict):
            return ack
        if set(ack) != {"operation_id", "code", "timestamp", "data"} or not isinstance(
            ack["code"], str
        ):
            return UNKNOWN
        data = ack["data"]
        if not isinstance(data, dict):
            return UNKNOWN
        data_fields = set(data)
        pair = (result.status_code, ack["code"])
        if pair in self.success:
            if self.endpoint == Endpoint.DECISIONS:
                registered = lookup(message.operation)
                contract = registered.decision_data if registered else None
                if contract is not None:
                    if pair != (200, "DECIDED"):
                        return UNKNOWN
                    try:
                        contract.validate_python(data)
                    except ValidationError:
                        return UNKNOWN
            if pair == (202, "PREPARE_ACCEPTED"):
                return Verdict(VerdictKind.DELIVERED, response=result.body)
            if data and pair != (200, "DECIDED"):
                return UNKNOWN
            return Verdict(VerdictKind.DELIVERED, response=result.body)
        if pair == (503, "UNAVAILABLE") and not data:
            return Verdict(VerdictKind.NOT_RECEIVED)
        if pair == (429, "BUSY"):
            wait = data.get("retry_after_ms")
            if data_fields == {"retry_after_ms"} and type(wait) is int and 1 <= wait <= 60000:
                return Verdict(VerdictKind.NOT_RECEIVED, retry_after_ms=wait)
            return UNKNOWN
        reason = data.get("reason_code")
        if not isinstance(reason, str):
            return UNKNOWN
        if (
            pair == (409, "CONFLICT")
            and data_fields == {"reason_code"}
            and reason in CONFLICT_REASONS
        ):
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.CONFLICT)
        if pair == (422, "REJECTED") and reason in REJECTION_REASONS:
            path = data.get("field_path")
            if data_fields == {"reason_code"} or (
                reason == "INVALID_DATA"
                and data_fields == {"reason_code", "field_path"}
                and isinstance(path, str)
                and 1 <= len(path) <= 256
                and re.fullmatch(r"/(?:[^~\ud800-\udfff]|~[01])*", path)
            ):
                return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
        return UNKNOWN


FACTS_PROTOCOL = "wms-facts"
DECISIONS_PROTOCOL = "wms-decisions"

# 适配器的唯一定义:入队服务与可靠发送器的注册共用这一份。
ADAPTERS = {
    FACTS_PROTOCOL: WmsEndpointAdapter(
        Endpoint.FACTS,
        "/api/v1/wes/facts",
        frozenset({(200, "RECORDED"), (200, "DUPLICATE")}),
    ),
    DECISIONS_PROTOCOL: WmsEndpointAdapter(
        Endpoint.DECISIONS,
        "/api/v1/wes/decisions",
        frozenset({(200, "DECIDED"), (202, "PREPARE_ACCEPTED")}),
    ),
}

PROTOCOL_OF = {adapter.endpoint: protocol for protocol, adapter in ADAPTERS.items()}
