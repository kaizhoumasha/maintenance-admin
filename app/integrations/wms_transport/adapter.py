from urllib.parse import unquote

from app.core.strict_json import json_object as _json_object
from app.http_client import HttpRequest, HttpResult
from app.integrations.wms_envelope import UNKNOWN, read_ack
from app.reliable import FailReason, OutgoingMessage, Verdict, VerdictKind
from app.reliable.config import TargetConfig

from .protocol import DEFAULT_SUBMIT_PATH, OPERATION, PROTOCOL, REJECTION_REASONS

MAX_PATH_LENGTH = 2048


def _validate_submit_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH_LENGTH:
        raise ValueError("invalid submit path")
    decoded_path = unquote(path)
    if (
        decoded_path.startswith("//")
        or any(char in path for char in "?#")
        or "\\" in decoded_path
        or any(ord(char) < 32 or ord(char) == 127 for char in path + decoded_path)
        or any(segment in (".", "..") for segment in decoded_path.split("/"))
    ):
        raise ValueError("invalid submit path")
    return path


def _requested_task_id(message: OutgoingMessage) -> str:
    request = _json_object(message.request_raw) or {}
    data = request.get("data")
    task_id = data.get("transport_task_id") if isinstance(data, dict) else None
    if (
        message.operation != OPERATION
        or request.get("operation") != OPERATION
        or request.get("operation_id") != message.operation_id
        or not isinstance(task_id, str)
        or not task_id
    ):
        raise ValueError("frozen submit identity mismatch")
    return task_id


class TransportSubmitAdapter:
    """合同 §4:`transport.task.submit@v1` 的原字节透传与同步 ACK 分类。

    业务字段(DTO、位置、面值)由能力层在冻结前校验,这里只核对身份自洽,不重复业务校验。
    """

    def __init__(self, submit_path: str = DEFAULT_SUBMIT_PATH):
        self.submit_path = _validate_submit_path(submit_path)

    def build_request(self, message: OutgoingMessage, target: TargetConfig) -> HttpRequest:
        _requested_task_id(message)
        return HttpRequest(
            method="POST",
            path=self.submit_path,
            headers=(("accept", "application/json"), ("content-type", "application/json")),
            body=message.request_raw,
        )

    def classify_response(self, message: OutgoingMessage, result: HttpResult) -> Verdict:
        task_id = _requested_task_id(message)
        ack = read_ack(message, result)
        if isinstance(ack, Verdict):
            return ack
        data = ack["data"]
        if data.get("transport_task_id") != task_id:
            return UNKNOWN
        pair = (result.status_code, ack.get("code"))
        if pair in ((202, "RECEIVED"), (200, "DUPLICATE")):
            return Verdict(VerdictKind.DELIVERED, response=result.body)
        if pair == (409, "CONFLICT"):
            return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.CONFLICT)
        if pair == (422, "REJECTED"):
            reason = data.get("reason_code")
            if isinstance(reason, str) and reason in REJECTION_REASONS:
                return Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED)
            return UNKNOWN
        if pair == (503, "UNAVAILABLE"):
            return Verdict(VerdictKind.NOT_RECEIVED)
        return UNKNOWN


# 适配器的唯一定义:默认 TransportService 的入队校验与可靠发送器的注册共用这一份。
ADAPTERS = {PROTOCOL: TransportSubmitAdapter()}
