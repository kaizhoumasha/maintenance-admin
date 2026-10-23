"""ECS 事件接收器:大小 → JSON → 信封 → 身份与先查重 → data 校验 → 写收据 → 处理器/未注册处理。

处理器在调用方打开的外层事务里运行:本模块不开保存点、不提交、不回滚,处理器异常原样上抛。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.strict_json import canonical_digest, json_object
from app.integrations.callback_diagnostics import queue_diagnostics
from app.integrations.wms_callback.receiver import LOCK_TIMEOUT, CallbackResponse
from app.models import CallbackReceipt

from .envelope import EcsEventEnvelope, issues_from
from .registry import EcsEventContext, lookup

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
    if len(json.dumps(note).encode()) <= MAX_DIAGNOSTIC_BYTES:
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
    for key in ("device_code", "event_type"):
        if isinstance(diagnostic.get(key), str):
            diagnostic[key] = render_untrusted(diagnostic[key])
    return {"operation": OPERATION, "operation_id": operation_id, "diagnostic": diagnostic}


async def _existing(session: AsyncSession, identity: str) -> CallbackReceipt | None:
    return await session.scalar(
        select(CallbackReceipt).where(
            CallbackReceipt.operation == OPERATION, CallbackReceipt.operation_id == identity
        )
    )


def _event(envelope: EcsEventEnvelope) -> dict:
    return envelope.model_dump(
        include={"device_code", "event_type", "timestamp", "is_debug", "data"}
    )


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
        raw_digest = canonical_digest(parsed)
    except (ValueError, RecursionError):  # 如 1e400 解析成 inf,永远无法成功,不能让 ECS 重试
        return _invalid(_INVALID_JSON, envelope)
    event_type, device_code = envelope.event_type, envelope.device_code

    def duplicate(existing: CallbackReceipt) -> EcsResponse:
        return EcsResponse(
            existing.status_code, existing.body, "duplicate", event_type, device_code, identity
        )

    existing = await _existing(session, identity)
    if existing is not None:  # 先查重:不再校验 data,处理器后来收紧校验也不影响原样重报
        return duplicate(existing)
    registered = lookup(event_type)
    data = envelope.data
    if registered is not None and registered.data_model is not None:
        try:
            data = registered.data_model.model_validate(envelope.data)
        except ValidationError as error:
            issues = issues_from(error, model=registered.data_model, prefix="data")
            return _invalid(issues, envelope)
    postgresql = session.get_bind().dialect.name == "postgresql"
    if postgresql:  # 等待行锁有上限:超时整体回滚并返回 503,ECS 原样重报
        await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    insert = pg_insert if postgresql else sqlite_insert
    receipt_id = await session.scalar(
        insert(CallbackReceipt)
        .values(
            operation=OPERATION,
            operation_id=identity,
            raw_digest=raw_digest,
            effective_digest=None,
            compare_mode="RAW",
            status_code=200,
            body=dict(ACK),
            resp_timestamp=unix_ms(datetime.now(UTC)),
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["operation", "operation_id"])
        .returning(CallbackReceipt.id)
    )
    if receipt_id is None:  # 并发的同身份事件先提交了
        existing = await _existing(session, identity)
        if existing is None:
            raise RuntimeError("receipt identity conflict without a visible receipt")
        return duplicate(existing)
    if registered is None:
        outcome, notes = "unregistered", [unregistered_note(_event(envelope))]
    else:
        ctx = EcsEventContext(
            session=session,
            receipt_id=receipt_id,
            digest=identity,
            device_code=device_code,
            event_type=event_type,
            timestamp=envelope.timestamp,
            is_debug=envelope.is_debug,
        )
        # 不捕获异常:控制器映射 503,整个事务(含收据)回滚
        await registered.func(ctx, data)
        outcome, notes = "received", ctx.notes
    if notes:
        await session.execute(
            update(CallbackReceipt).where(CallbackReceipt.id == receipt_id).values(diagnostic=notes)
        )
        queue_diagnostics(session, [_alert(identity, note) for note in notes])
    return EcsResponse(200, dict(ACK), outcome, event_type, device_code, identity)
