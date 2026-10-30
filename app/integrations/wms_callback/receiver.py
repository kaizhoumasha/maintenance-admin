import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.identifiers import validate_uuid7
from app.core.strict_json import canonical_digest as _digest
from app.core.strict_json import json_object
from app.integrations.callback_diagnostics import queue_diagnostics
from app.integrations.callback_receipts import LOCK_TIMEOUT, find_receipt, insert_receipt
from app.models import CallbackReceipt
from app.transactions import after_commit

from .registry import (
    OUTCOMES,
    Accepted,
    CallbackContext,
    Conflict,
    Duplicate,
    InvalidPayload,
    Rejected,
    lookup,
)

MAX_BODY_BYTES = 256 * 1024
MAX_OPERATION_CHARS = 256


@dataclass(frozen=True)
class CallbackResponse:
    status_code: int
    body: dict | None
    outcome: str


def _identity(payload: dict | None) -> str | None:
    try:
        return validate_uuid7((payload or {}).get("operation_id"))
    except ValueError:
        return None


def _reply(status: int, operation_id: str, code: str, data: dict, outcome: str) -> CallbackResponse:
    return CallbackResponse(
        status,
        dict(operation_id=operation_id, code=code, timestamp=unix_ms(datetime.now(UTC)), data=data),
        outcome,
    )


def _usable_operation(value: object) -> bool:
    if not isinstance(value, str) or "\x00" in value or len(value) > MAX_OPERATION_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:  # 孤立代理字符无法存为 UTF-8
        return False
    return True


def _unavailable(operation_id: str | None) -> CallbackResponse:
    if operation_id is None:
        return CallbackResponse(503, None, "error")
    return _reply(503, operation_id, "UNAVAILABLE", {}, "error")


def unavailable(raw: bytes) -> CallbackResponse:
    return _unavailable(_identity(json_object(raw)))


def _replay(existing: CallbackReceipt) -> CallbackResponse:
    body = dict(existing.body)
    if body["code"] == "RECEIVED":
        body["code"] = "DUPLICATE"
        return CallbackResponse(200, body, "duplicate")
    return CallbackResponse(existing.status_code, body, "replay")


def _answer(outcome: object, operation_id: str) -> CallbackResponse:
    if isinstance(outcome, Accepted):
        return _reply(202, operation_id, "RECEIVED", outcome.data, "received")
    if isinstance(outcome, Duplicate):
        return _reply(200, operation_id, "DUPLICATE", outcome.data, "duplicate")
    if isinstance(outcome, Conflict):
        return _reply(409, operation_id, "CONFLICT", outcome.data, "conflict")
    return _reply(422, operation_id, "REJECTED", {"reason_code": outcome.reason_code}, "rejected")


async def _handle(
    session: AsyncSession,
    handler,
    receipt_id: int,
    operation: str,
    operation_id: str,
    timestamp: int,
    parsed: dict,
    *,
    runtime: object | None = None,
    on_committed=None,
    on_business_committed=None,
    on_owner_committed=None,
) -> CallbackResponse:
    """保存点内执行处理器:Rejected 显式回滚其写入;先构造并校验响应,再写回收据(同一事务)。"""
    async with session.begin_nested() as savepoint:
        context = CallbackContext(
            session,
            receipt_id,
            operation_id,
            timestamp,
            parsed,
            runtime=runtime,
            registration_source=handler.registration_source,
            operation=operation,
            on_owner_committed=on_owner_committed,
        )
        try:
            outcome = await handler.handle(context)
        except OUTCOMES as signal:  # 处理器 raise 的结果与 return 等价
            outcome = signal
        shared = (
            not handler.registration_source.local_only
            and handler.registration_source.module.startswith("app.businesses._components.")
        )
        if shared and isinstance(outcome, Accepted) and context.business_code is None:
            outcome = Rejected("INVALID_EVIDENCE")
        if not isinstance(outcome, OUTCOMES):
            raise TypeError(f"handler returned {type(outcome).__name__}, not an Outcome")
        if not isinstance(outcome, (Accepted, Duplicate, Conflict)):
            await savepoint.rollback()
    response = _answer(outcome, operation_id)
    json.dumps(response.body, allow_nan=False)  # 提交前确认可序列化,失败则整体回滚
    await session.execute(
        update(CallbackReceipt)
        .where(CallbackReceipt.id == receipt_id)
        .values(
            status_code=response.status_code,
            body=response.body,
            resp_timestamp=response.body["timestamp"],
            diagnostic=context.notes or None,
        )
    )
    if context.notes:
        queue_diagnostics(
            session,
            [
                {
                    "operation": operation,
                    "operation_id": operation_id,
                    "diagnostic": {key: value for key, value in note.items() if key != "data"},
                }
                for note in context.notes
            ],
        )
    if response.outcome == "received":
        if shared and on_business_committed is not None:
            after_commit(session, partial(on_business_committed, context.business_code))
        elif not shared and on_committed is not None:
            after_commit(session, partial(on_committed, handler.registration_source))
    return response


async def receive(
    session: AsyncSession,
    raw: bytes,
    *,
    runtime: object | None = None,
    on_committed=None,
    on_business_committed=None,
    on_owner_committed=None,
) -> CallbackResponse:
    if len(raw) > MAX_BODY_BYTES:
        return CallbackResponse(413, None, "too_large")
    payload = json_object(raw)
    operation_id = _identity(payload)
    if operation_id is None:
        return CallbackResponse(400, None, "invalid")
    operation = payload.get("operation")
    if not _usable_operation(operation):
        return _reply(
            422, operation_id, "REJECTED", {"reason_code": "INVALID_EVIDENCE"}, "rejected"
        )
    try:
        digest = _digest(payload)
    except (ValueError, RecursionError):
        return CallbackResponse(400, None, "invalid")
    timestamp = payload.get("timestamp")
    valid_envelope = (
        type(timestamp) is int
        and 0 <= timestamp <= 2**63 - 1
        and isinstance(payload.get("data"), dict)
    )
    handler = lookup(operation)
    parsed, reason = None, None
    if not valid_envelope:
        reason = "INVALID_EVIDENCE"
    elif handler is None:
        reason = "UNSUPPORTED_OPERATION"
    else:
        try:
            parsed = handler.parse(payload["data"])
        except InvalidPayload as error:
            reason = error.reason_code
    effective = (
        None
        if parsed is None
        else _digest(
            dict(operation=operation, operation_id=operation_id, timestamp=timestamp, data=parsed)
        )
    )
    # 已接纳路径先写临时响应占住身份,处理器返回后在同一事务内覆盖;事务提交前对外不可见。
    echo_for = handler.echo if handler is not None else None
    echo = echo_for(payload["data"]) if echo_for and valid_envelope else {}
    reply = (
        _reply(422, operation_id, "REJECTED", {"reason_code": reason, **echo}, "rejected")
        if reason
        else _reply(202, operation_id, "RECEIVED", {}, "received")
    )
    receipt_id = await insert_receipt(
        session,
        lock_timeout=LOCK_TIMEOUT,
        operation=operation,
        operation_id=operation_id,
        raw_digest=digest,
        effective_digest=effective,
        compare_mode="RAW" if effective is None else "EFFECTIVE",
        status_code=reply.status_code,
        body=reply.body,
        resp_timestamp=reply.body["timestamp"],
        created_at=datetime.now(UTC),
    )
    if receipt_id is not None:
        if reason:
            return reply
        response = await _handle(
            session,
            handler,
            receipt_id,
            operation,
            operation_id,
            timestamp,
            parsed,
            runtime=runtime,
            on_committed=on_committed,
            on_business_committed=on_business_committed,
            on_owner_committed=on_owner_committed,
        )
        return response
    existing = await find_receipt(session, operation, operation_id)
    if existing is None:
        raise RuntimeError("receipt identity conflict without a visible receipt")
    if existing.raw_digest == digest:
        return _replay(existing)
    if existing.compare_mode == "EFFECTIVE":
        if handler is None:  # 规范化器不可用,无法可靠比较,不能重新判为未知 operation
            return _unavailable(operation_id)
        if effective is not None and existing.effective_digest == effective:
            return _replay(existing)
    # 409 回显首次响应里对端要求的键(如首次消息已解析出的任务编号);首次没有就是 {}
    first = existing.body.get("data") or {}
    echo_first = echo_for(first) if echo_for else {}
    return _reply(409, operation_id, "CONFLICT", echo_first, "conflict")
