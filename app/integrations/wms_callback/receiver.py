import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType

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
    Unavailable,
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
    if isinstance(outcome, Unavailable):
        return _unavailable(operation_id)
    if isinstance(outcome, Conflict):
        return _reply(409, operation_id, "CONFLICT", outcome.data, "conflict")
    return _reply(422, operation_id, "REJECTED", {"reason_code": outcome.reason_code}, "rejected")


class _UnstoredUnavailable(Exception):
    def __init__(self, response: CallbackResponse):
        self.response = response


def _readonly(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_readonly(item) for item in value)
    return value


def _context(
    session: AsyncSession,
    handler,
    receipt_id: int,
    operation: str,
    operation_id: str,
    timestamp: int,
    parsed: dict,
    *,
    runtime,
    on_owner_committed,
    original_response: Mapping | None = None,
) -> CallbackContext:
    return CallbackContext(
        session,
        receipt_id,
        operation_id,
        timestamp,
        parsed,
        runtime=runtime,
        registration_source=handler.registration_source,
        operation=operation,
        on_owner_committed=on_owner_committed,
        original_response=original_response,
    )


async def _guard(handler, context: CallbackContext) -> CallbackResponse | None:
    if handler.archive_guard is None:
        return None
    payload = handler.data_model.model_validate(context.parsed, strict=False)
    try:
        response = await handler.archive_guard(context, payload)
    except (Rejected, Conflict) as signal:
        return _answer(signal, context.operation_id)
    except Unavailable:
        raise _UnstoredUnavailable(_unavailable(context.operation_id)) from None
    if isinstance(response, CallbackResponse) and 500 <= response.status_code < 600:
        raise _UnstoredUnavailable(_unavailable(context.operation_id))
    if response is not None and (
        not isinstance(response, CallbackResponse) or response.status_code < 400
    ):
        raise TypeError("archive_guard must return a rejection CallbackResponse or None")
    return response


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
    context: CallbackContext | None = None,
    original_response: Mapping | None = None,
    replay_response: CallbackResponse | None = None,
) -> CallbackResponse:
    """Run original acceptance or an explicitly registered recheck in its savepoint."""
    context = context or _context(
        session,
        handler,
        receipt_id,
        operation,
        operation_id,
        timestamp,
        parsed,
        runtime=runtime,
        on_owner_committed=on_owner_committed,
    )
    async with session.begin_nested() as savepoint:
        try:
            if original_response is None:
                outcome = await handler.handle(context)
            else:
                payload = handler.data_model.model_validate(parsed, strict=False)
                outcome = await handler.recheck(context, payload, original_response)
                if outcome is None:
                    await savepoint.rollback()
                    return replay_response
        except OUTCOMES as signal:
            outcome = signal
        shared = (
            not handler.registration_source.local_only
            and handler.registration_source.module.startswith("app.businesses._components.")
        )
        if shared and isinstance(outcome, Accepted) and context.business_code is None:
            outcome = Rejected("INVALID_EVIDENCE")
        if not isinstance(outcome, OUTCOMES):
            raise TypeError(f"handler returned {type(outcome).__name__}, not an Outcome")
        pending = (
            isinstance(outcome, Unavailable)
            and handler.recheck is not None
            and context.business_code is not None
        )
        if not isinstance(outcome, (Accepted, Duplicate, Conflict)) and not pending:
            await savepoint.rollback()
        if isinstance(outcome, Unavailable) and not pending:
            raise _UnstoredUnavailable(_unavailable(operation_id))
    response = _answer(outcome, operation_id)
    json.dumps(response.body, allow_nan=False)
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


async def _receive(
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
        reason = (
            handler.errors.invalid_envelope if handler and handler.errors else "INVALID_EVIDENCE"
        )
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
        context = _context(
            session,
            handler,
            receipt_id,
            operation,
            operation_id,
            timestamp,
            parsed,
            runtime=runtime,
            on_owner_committed=on_owner_committed,
        )
        guarded = await _guard(handler, context)
        if guarded is not None:
            await session.execute(
                update(CallbackReceipt)
                .where(CallbackReceipt.id == receipt_id)
                .values(
                    status_code=guarded.status_code,
                    body=guarded.body,
                    resp_timestamp=guarded.body["timestamp"],
                )
            )
            return guarded
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
            context=context,
        )
        return response
    existing = await find_receipt(
        session,
        operation,
        operation_id,
        for_update=handler is not None and handler.recheck is not None,
    )
    if existing is None:
        raise RuntimeError("receipt identity conflict without a visible receipt")
    same = existing.raw_digest == digest
    if not same and existing.compare_mode == "EFFECTIVE":
        if handler is None:
            return _unavailable(operation_id)
        same = effective is not None and existing.effective_digest == effective
    if not same:
        first = existing.body.get("data") or {}
        echo_first = echo_for(first) if echo_for else {}
        if handler is not None and handler.errors is not None:
            echo_first = {**echo_first, "reason_code": handler.errors.identity_conflict}
        return _reply(409, operation_id, "CONFLICT", echo_first, "conflict")
    if handler is not None and parsed is not None:
        context = _context(
            session,
            handler,
            existing.id,
            operation,
            operation_id,
            timestamp,
            parsed,
            runtime=runtime,
            on_owner_committed=on_owner_committed,
            original_response=_readonly(existing.body),
        )
        guarded = await _guard(handler, context)
        if guarded is not None:
            return guarded
        if handler.recheck is not None and existing.body["code"] not in {"RECEIVED", "DUPLICATE"}:
            return await _handle(
                session,
                handler,
                existing.id,
                operation,
                operation_id,
                timestamp,
                parsed,
                runtime=runtime,
                on_committed=on_committed,
                on_business_committed=on_business_committed,
                on_owner_committed=on_owner_committed,
                context=context,
                original_response=context.original_response,
                replay_response=_replay(existing),
            )
    return _replay(existing)


async def receive(
    session: AsyncSession,
    raw: bytes,
    *,
    runtime: object | None = None,
    on_committed=None,
    on_business_committed=None,
    on_owner_committed=None,
) -> CallbackResponse:
    options = dict(
        runtime=runtime,
        on_committed=on_committed,
        on_business_committed=on_business_committed,
        on_owner_committed=on_owner_committed,
    )
    payload = json_object(raw) if len(raw) <= MAX_BODY_BYTES else None
    operation = payload.get("operation") if payload else None
    handler = lookup(operation) if isinstance(operation, str) else None
    if handler is None or _identity(payload) is None:
        return await _receive(session, raw, **options)
    if session.get_bind().dialect.name == "sqlite":
        connection = await session.connection()
        raw_connection = await connection.get_raw_connection()
        # Legacy SQLite SAVEPOINT must not become the root transaction and commit early.
        if not raw_connection.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    try:
        async with session.begin_nested():
            return await _receive(session, raw, **options)
    except _UnstoredUnavailable as signal:
        return signal.response
