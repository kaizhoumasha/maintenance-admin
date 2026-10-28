"""WMS 合同 §4 所有 operation 通用的响应前置检查,decisions、facts 与搬运提交共用。"""

from app.core.strict_json import canonical_digest, json_object
from app.http_client import DeliveryState, HttpResult
from app.reliable import FailReason, OutgoingMessage, ProtocolAdapter, Verdict, VerdictKind

INT64_MAX = 2**63 - 1
UNKNOWN = Verdict(VerdictKind.UNKNOWN)


def read_ack(message: OutgoingMessage, result: HttpResult) -> dict | Verdict:
    """合同 §4 对所有 operation 通用的响应前置检查,decisions、facts 与搬运提交共用。

    返回已确认回显 operation_id、带 int64 毫秒时间戳、`data` 为对象的 ACK;信封不成立返回
    `UNKNOWN`,没有响应体的 400、413 是对端终局拒绝。调用方再按各自的状态码与 code 表映射。
    """
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
    try:
        # JSON 大指数可能解码为 inf;不能把失真的数值作为有效响应暴露。
        canonical_digest(ack)
    except (ValueError, RecursionError):
        return UNKNOWN
    timestamp = ack.get("timestamp")
    if (
        ack.get("operation_id") != message.operation_id
        or type(timestamp) is not int
        or not 0 <= timestamp <= INT64_MAX
        or not isinstance(ack.get("data"), dict)
    ):
        return UNKNOWN
    return ack


RESPONSE_COLUMNS = (
    "id",
    "target",
    "operation",
    "operation_id",
    "request_raw",
    "response_raw",
    "status",
    "fail_reason",
    "last_http_status",
    "last_verdict",
    "last_failure_kind",
    "delivery_uncertain",
    "business_key",
    "trace_id",
    "created_at",
    "expires_at",
)


def outgoing_message(row: dict) -> OutgoingMessage:
    return OutgoingMessage(
        row["target"],
        row["operation"],
        row["operation_id"],
        row["request_raw"],
        row["business_key"],
        row["trace_id"],
        row["created_at"],
        row["expires_at"],
    )


# Pure projection of a stored terminal response; never reads retry/last response bytes.
def stored_response(message: OutgoingMessage, row: dict, adapter: ProtocolAdapter | None) -> dict:
    fields = dict(
        response_state="NONE",
        http_status=None,
        response_code=None,
        response_timestamp=None,
        response_data=None,
    )
    if row["status"] not in {"DONE", "FAILED"}:
        return fields
    if row["status"] == "FAILED" and row["fail_reason"] == "LOCAL_ERROR":
        return fields
    raw = row["response_raw"]
    if raw is None or adapter is None:
        return {**fields, "response_state": "UNAVAILABLE"}
    result = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE, None, row["last_http_status"], (), raw
    )
    try:
        verdict = adapter.classify_response(message, result)
    except ValueError:
        return {**fields, "response_state": "INVALID"}
    expected = VerdictKind.DELIVERED if row["status"] == "DONE" else VerdictKind.PEER_TERMINAL
    if (
        verdict.kind != expected
        or row["last_failure_kind"] is not None
        or row["last_verdict"] != expected
        or (expected == VerdictKind.PEER_TERMINAL and verdict.reason != row["fail_reason"])
    ):
        return {**fields, "response_state": "INVALID"}
    ack = json_object(raw)
    return dict(
        response_state="VALID",
        http_status=result.status_code,
        response_code=ack["code"] if ack else None,
        response_timestamp=ack["timestamp"] if ack else None,
        response_data=ack["data"] if ack else None,
    )
