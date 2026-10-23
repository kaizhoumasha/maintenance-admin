"""ECS 事件接收器:大小 → JSON → 信封 → 身份与先查重 → 登记处理器的 data 校验。

收据写入、处理器执行与未注册类型处理由后续任务接在 `_accept` 处。
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.strict_json import canonical_digest, json_object
from app.integrations.wms_callback.receiver import CallbackResponse
from app.models import CallbackReceipt

from .envelope import EcsEventEnvelope, issues_from
from .registry import lookup

MAX_BODY_BYTES = 256 * 1024
ACK = {"code": 200, "message": "ACK"}
OPERATION = "ecs.event"


@dataclass(frozen=True)
class EcsResponse(CallbackResponse):
    event_type: str | None = None
    device_code: str | None = None
    digest: str | None = None  # 信封无效时为 None


def event_identity(envelope: EcsEventEnvelope) -> str:
    return canonical_digest(
        dict(
            device_code=envelope.device_code,
            event_type=envelope.event_type,
            timestamp=envelope.timestamp,
            is_debug=envelope.is_debug,
            data=envelope.data,
        )
    )


def _invalid(issues: list[dict], envelope: EcsEventEnvelope | None = None) -> EcsResponse:
    body = {"code": 400, "message": "INVALID_ENVELOPE", "error_detail": {"issues": issues}}
    return EcsResponse(
        400,
        body,
        "invalid",
        envelope.event_type if envelope else None,
        envelope.device_code if envelope else None,
    )


_INVALID_JSON = [{"field": "$", "code": "INVALID_JSON"}]


async def receive_ecs_event(session: AsyncSession, raw: bytes) -> EcsResponse:
    if len(raw) > MAX_BODY_BYTES:
        return EcsResponse(413, {"code": 413, "message": "PAYLOAD_TOO_LARGE"}, "too_large")
    parsed = json_object(raw)
    if parsed is None:
        return _invalid(_INVALID_JSON)
    try:
        envelope = EcsEventEnvelope.model_validate(parsed)
    except ValidationError as error:
        return _invalid(issues_from(error, model=EcsEventEnvelope))
    try:
        identity = event_identity(envelope)
        canonical_digest(parsed)  # 与收据的 raw_digest 同一算法;此处只确认可序列化
    except (ValueError, RecursionError):  # 如 1e400 解析成 inf,永远无法成功,不能让 ECS 重试
        return _invalid(_INVALID_JSON, envelope)
    event_type, device_code = envelope.event_type, envelope.device_code
    existing = await session.scalar(
        select(CallbackReceipt).where(
            CallbackReceipt.operation == OPERATION, CallbackReceipt.operation_id == identity
        )
    )
    if existing is not None:  # 先查重:不再校验 data,处理器后来收紧校验也不影响原样重报
        return EcsResponse(
            existing.status_code, existing.body, "duplicate", event_type, device_code, identity
        )
    registered = lookup(event_type)
    if registered is not None and registered.data_model is not None:
        try:
            registered.data_model.model_validate(envelope.data)
        except ValidationError as error:
            issues = issues_from(error, model=registered.data_model, prefix="data")
            return _invalid(issues, envelope)
    # 接缝:写收据、执行处理器、未注册类型的处理在此接入
    return EcsResponse(200, dict(ACK), "received", event_type, device_code, identity)
