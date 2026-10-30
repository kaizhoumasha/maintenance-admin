"""ECS 事件接收器:大小 → JSON → 信封 → 身份与先查重 → data 校验 → 写收据 → 处理器/未注册处理。

处理器在调用方打开的外层事务里运行:本模块不开保存点、不提交、不回滚,处理器异常原样上抛。
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.strict_json import canonical_digest, json_object
from app.integrations.callback_diagnostics import queue_diagnostics
from app.integrations.callback_receipts import find_receipt, insert_receipt
from app.integrations.wms_callback.receiver import CallbackResponse
from app.models import CallbackReceipt

from .binding import BindingResolver
from .envelope import EcsEventEnvelope, issues_from
from .registry import EcsEventContext, lookup

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024
MAX_DIAGNOSTIC_BYTES = 32 * 1024
PREVIEW_BYTES = 1024
IDENTITY_FIELD_CHARS = 128
LOG_FIELD_CHARS = 64
ACK = {"code": 200, "message": "ACK"}
OPERATION = "ecs.event"


@dataclass(frozen=True)
class EcsResponse(CallbackResponse):
    event_type: str | None = None
    device_code: str | None = None
    digest: str | None = None  # 信封无效时为 None
    diagnostic: list[dict] | None = None


def event_identity(envelope: EcsEventEnvelope) -> str:
    return canonical_digest(_event(envelope))


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


def render_untrusted(value: str, limit: int = LOG_FIELD_CHARS) -> str:
    """日志里的不可信字符串:先截断再 repr(换行被转义),被截断时加省略号。"""
    return repr(value[:limit]) + ("..." if len(value) > limit else "")


def unregistered_note(event: dict) -> dict:
    """未注册事件的诊断;整条序列化后不超过 MAX_DIAGNOSTIC_BYTES,超限时只留摘要、大小、预览。"""
    note = {
        "reason_code": "UNREGISTERED_EVENT_TYPE",
        "device_code": event["device_code"],
        "event_type": event["event_type"],
        "data": event,
    }
    if len(json.dumps([note]).encode()) <= MAX_DIAGNOSTIC_BYTES:  # 落库的是 [note]
        return note
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))  # ASCII,按字节切不会断字符
    return {
        "reason_code": "UNREGISTERED_EVENT_TYPE",
        "device_code": event["device_code"][:IDENTITY_FIELD_CHARS],
        "event_type": event["event_type"][:IDENTITY_FIELD_CHARS],
        "data": {
            "digest": canonical_digest(event),
            "size": len(canonical),
            "preview": canonical[:PREVIEW_BYTES],
        },
    }


def _alert(operation_id: str, note: dict) -> dict:
    """告警日志副本:无 data,不可信字段经 render_untrusted;顶层键固定,避免撞 LogRecord 保留名。"""
    diagnostic = {key: value for key, value in note.items() if key != "data"}
    for key, value in diagnostic.items():
        if key != "reason_code" and isinstance(value, str):
            diagnostic[key] = render_untrusted(value)
    return {"operation": OPERATION, "operation_id": operation_id, "diagnostic": diagnostic}


def _event(envelope: EcsEventEnvelope) -> dict:
    return envelope.model_dump(include={"device_code", "event_type", "timestamp", "data"})


async def receive_ecs_event(
    session: AsyncSession,
    raw: bytes,
    *,
    resolve_binding: BindingResolver | None = None,
    runtime: object | None = None,
) -> EcsResponse:
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
        legacy_identities = [
            canonical_digest({**_event(envelope), "is_debug": debug}) for debug in (False, True)
        ]
        raw_digest = canonical_digest(parsed)
    except (ValueError, RecursionError):  # 如 1e400 解析成 inf,永远无法成功,不能让 ECS 重试
        return _invalid(_INVALID_JSON, envelope)
    event_type, device_code = envelope.event_type, envelope.device_code

    def duplicate(existing: CallbackReceipt) -> EcsResponse:
        return EcsResponse(
            existing.status_code,
            existing.body,
            "duplicate",
            event_type,
            device_code,
            existing.operation_id,
            existing.diagnostic,
        )

    existing = await find_receipt(session, OPERATION, identity)
    if existing is not None:  # 先查重:不再校验 data,处理器后来收紧校验也不影响原样重报
        return duplicate(existing)
    # 旧身份只用于查重,绝不重写原行或作为新消息身份。
    historical = []
    for legacy_identity in legacy_identities:
        receipt = await find_receipt(session, OPERATION, legacy_identity)
        if receipt is not None:
            historical.append(receipt)
    if historical:
        return duplicate(min(historical, key=lambda receipt: (receipt.created_at, receipt.id)))
    try:
        if "\x00" in device_code:
            raise ValueError("device_code must not contain NUL")
        device_code.encode("utf-8")
    except ValueError:
        return _invalid([{"field": "device_code", "code": "INVALID_VALUE"}], envelope)
    binding = await resolve_binding(session, device_code) if resolve_binding else None
    if binding is not None and binding.device_code != device_code:
        raise RuntimeError("ECS binding does not match event device")
    registered = lookup(binding.device_role, event_type) if binding else None
    data = envelope.data
    if registered is not None and registered.data_model is not None:
        try:
            data = registered.data_model.model_validate(envelope.data)
        except ValidationError as error:
            issues = issues_from(error, model=registered.data_model, prefix="data")
            return _invalid(issues, envelope)
    receipt_id = await insert_receipt(
        session,
        operation=OPERATION,
        operation_id=identity,
        device_code=envelope.device_code,
        raw_digest=raw_digest,
        effective_digest=None,
        compare_mode="RAW",
        status_code=200,
        body=dict(ACK),
        resp_timestamp=unix_ms(datetime.now(UTC)),
        created_at=datetime.now(UTC),
    )
    if receipt_id is None:  # 并发的同身份事件先提交了
        existing = await find_receipt(session, OPERATION, identity)
        if existing is None:
            raise RuntimeError("receipt identity conflict without a visible receipt")
        return duplicate(existing)
    if registered is None:
        note = unregistered_note(_event(envelope))
        if isinstance(note["data"], dict) and "digest" in note["data"]:  # 已缩减:完整事件不会落库
            logger.error(
                "oversized unregistered ECS event reduced: digest=%s size=%s device_code=%s "
                "event_type=%s",
                note["data"]["digest"],
                note["data"]["size"],
                render_untrusted(device_code),
                render_untrusted(event_type),
            )
        outcome, notes = "unregistered", [note]
    else:
        ctx = EcsEventContext(
            session=session,
            receipt_id=receipt_id,
            digest=identity,
            device_code=device_code,
            event_type=event_type,
            timestamp=envelope.timestamp,
            binding=binding,
            runtime=runtime,
        )
        # 不捕获异常:控制器映射 503,整个事务(含收据)回滚
        await registered.func(ctx, data)
        outcome, notes = "received", ctx.notes
    if notes:
        await session.execute(
            update(CallbackReceipt).where(CallbackReceipt.id == receipt_id).values(diagnostic=notes)
        )
        queue_diagnostics(session, [_alert(identity, note) for note in notes])
    return EcsResponse(200, dict(ACK), outcome, event_type, device_code, identity, notes or None)
