"""decisions / facts 端点的协议适配器:信封核对与公共响应联合(合同 §4.4)的分类,不含任何业务字段。"""

from app.core.strict_json import json_object
from app.http_client import DeliveryState, HttpRequest, HttpResult
from app.reliable import FailReason, OutgoingMessage, Verdict, VerdictKind
from app.reliable.config import TargetConfig

from .discovery import load_operation_modules
from .operation import Endpoint, lookup

INT64_MAX = 2**63 - 1
UNKNOWN = Verdict(VerdictKind.UNKNOWN)
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
    202 PREPARE_ACCEPTED。成功即 DELIVERED 并保留响应原文,解释响应是各 operation 的事。"""

    def __init__(self, endpoint: Endpoint, path: str, success: frozenset[tuple[int, str]]):
        self.endpoint, self.path, self.success = endpoint, path, success

    def build_request(self, message: OutgoingMessage, target: TargetConfig) -> HttpRequest:
        # 重启后发送器可能在任何 `send` 之前就领取存量消息,所以注册表要在这里保证已加载
        load_operation_modules()
        request = json_object(message.request_raw) or {}
        registered = lookup(message.operation)
        if (
            request.get("operation") != message.operation
            or request.get("operation_id") != message.operation_id
            or registered is None
            or registered.endpoint != self.endpoint
        ):
            raise ValueError("frozen request does not match this endpoint")
        return HttpRequest(
            method="POST",
            path=self.path,
            headers=(("accept", "application/json"), ("content-type", "application/json")),
            body=message.request_raw,
        )

    def classify_response(self, message: OutgoingMessage, result: HttpResult) -> Verdict:
        if (
            result.delivery_state != DeliveryState.HTTP_RESPONSE_COMPLETE
            or result.failure_kind is not None
            or not isinstance(result.body, bytes)
        ):
            return UNKNOWN
        if result.status_code in (400, 413) and result.body == b"":
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
        ack = json_object(result.body)
        if ack is None:
            return UNKNOWN
        timestamp, data = ack.get("timestamp"), ack.get("data")
        if (
            ack.get("operation_id") != message.operation_id
            or type(timestamp) is not int
            or not 0 <= timestamp <= INT64_MAX
            or not isinstance(data, dict)
        ):
            return UNKNOWN
        pair = (result.status_code, ack.get("code"))
        if pair in self.success:
            return Verdict(VerdictKind.DELIVERED, response=result.body)
        if pair == (503, "UNAVAILABLE"):
            return Verdict(VerdictKind.NOT_RECEIVED)
        if pair == (429, "BUSY"):
            wait = data.get("retry_after_ms")
            if type(wait) is int and 1 <= wait <= 60000:
                return Verdict(VerdictKind.NOT_RECEIVED, retry_after_ms=wait)
            return UNKNOWN
        if pair == (409, "CONFLICT") and data.get("reason_code") in CONFLICT_REASONS:
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.CONFLICT)
        if pair == (422, "REJECTED") and data.get("reason_code") in REJECTION_REASONS:
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
