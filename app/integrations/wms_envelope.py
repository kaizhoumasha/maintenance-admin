"""WMS 合同 §4 所有 operation 通用的响应前置检查,decisions、facts 与搬运提交共用。"""

from app.core.strict_json import json_object
from app.http_client import DeliveryState, HttpResult
from app.reliable import FailReason, OutgoingMessage, Verdict, VerdictKind

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
    timestamp = ack.get("timestamp")
    if (
        ack.get("operation_id") != message.operation_id
        or type(timestamp) is not int
        or not 0 <= timestamp <= INT64_MAX
        or not isinstance(ack.get("data"), dict)
    ):
        return UNKNOWN
    return ack
