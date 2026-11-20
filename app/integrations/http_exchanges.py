"""Persist outbound runtime facts, then project them in an independent transaction."""

import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import case, delete, false, func, inspect, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from app.core.identifiers import validate_uuid7
from app.core.strict_json import json_object
from app.http_client import DeliveryState, HttpResult
from app.integrations.runtime_authority import enabled
from app.models.http_exchange import (
    EcsHttpExchange,
    ReliableDeliveryAttempt,
    TransportHttpExchange,
    WmsHttpExchange,
    http_projection_pending,
)
from app.models.integration_history import IntegrationRecord
from app.models.reliable import ReliableMessage

logger = logging.getLogger(__name__)

OUTBOUND = {
    "wms-decisions": (WmsHttpExchange, "DECISION"),
    "wms-facts": (WmsHttpExchange, "FACT"),
    "wms-transport": (TransportHttpExchange, "SUBMIT"),
    "ecs-command": (EcsHttpExchange, "COMMAND"),
}
WMS_PROTOCOLS = ("wms-decisions", "wms-facts", "wms-transport")
REQUEST_HEADERS = {"content-type", "accept", "content-encoding"}
RESPONSE_HEADERS = {"content-type", "content-encoding", "retry-after"}


def safe_text(value, limit=512):
    if (
        isinstance(value, str)
        and len(value) <= limit
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
    ):
        try:
            value.encode("utf-8")
        except UnicodeError:
            return None
        return value
    return None


def safe_json(value):
    """PostgreSQL JSONB rejects NUL and unpaired surrogate code points."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and (
            "\x00" in item or any(0xD800 <= ord(c) <= 0xDFFF for c in item)
        ):
            return None
        elif type(item) is int and item.bit_length() > 4096:
            return None
        elif type(item) is float and not (-float("inf") < item < float("inf")):
            return None
    return value


def headers(values, allowed):
    result = {}
    for key, value in values:
        if key.lower() in allowed and safe_text(value) is not None:
            result.setdefault(key.lower(), []).append(value)
    return result or None


def operation_uuid(value):
    try:
        return UUID(validate_uuid7(value))
    except ValueError:
        return None


def body_fields(side, wire, state, payload):
    return {
        f"{side}_wire": wire,
        f"{side}_wire_state": state,
        f"{side}_wire_sha256": sha256(wire).hexdigest() if wire is not None else None,
        f"{side}_wire_bytes": len(wire) if wire is not None else None,
        f"{side}_payload": safe_json(payload) if state == "FULL" else None,
    }


async def open_attempt(session, *, claim, protocol, at, identity):
    request = claim.request
    scoped_static = claim.message.scope is not None and claim.message.delivery_snapshot is None
    if not enabled() and not scoped_static and (protocol not in OUTBOUND or request is None):
        return
    key = (claim.message_id, claim.lease_id)
    if await session.get(ReliableDeliveryAttempt, key) is not None:
        return
    message = claim.message
    session.add(
        ReliableDeliveryAttempt(
            id=identity,
            message_id=claim.message_id,
            lease_id=claim.lease_id,
            protocol=protocol,
            exchange_id=message.exchange_id,
            request_record_id=message.request_record_id,
            operation=message.operation,
            operation_id=message.operation_id,
            business_key=message.business_key,
            trace_id=message.trace_id,
            peer_endpoint=claim.dispatch_origin or message.target,
            traffic_source="MOCK"
            if request and request.path.startswith("/api/v1/dev/")
            else "PEER",
            http_method=request.method if request else "POST",
            endpoint_path=request.path if request else "",
            request_headers=headers(request.headers, REQUEST_HEADERS) if request else None,
            request_wire=message.request_raw if enabled() else None,
            started_at=at,
        )
    )


async def close_attempt(session, *, message_id, lease_id, at, reason, outcome=None):
    if lease_id is None:
        return
    attempt = await session.get(
        ReliableDeliveryAttempt, (message_id, lease_id), with_for_update=True
    )
    if attempt is None or attempt.finished_at is not None:
        return
    result = outcome.result if outcome else None
    if result is None and attempt.response_record_id is not None:
        state = attempt.response_state
    elif result is not None and result.delivery_state == DeliveryState.NOT_SENT:
        state = "NOT_SENT"
    elif result is not None and result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE:
        state = "RESPONSE_OBSERVED"
    elif result is not None and result.status_code is not None:
        state = "RESPONSE_INCOMPLETE"
    else:
        state = "DELIVERY_UNKNOWN"
    attempt.finished_at = at
    attempt.close_reason = reason
    attempt.attempt_state = state or "DELIVERY_UNKNOWN"
    attempt.failure_kind = (
        result.failure_kind
        if result
        else ((outcome.fail_reason or outcome.pause_reason) if outcome else None)
    )
    attempt.revision += 1


async def record_late_not_sent(session, *, claim, failure_kind):
    attempt = await session.get(
        ReliableDeliveryAttempt, (claim.message_id, claim.lease_id), with_for_update=True
    )
    if (
        attempt is None
        or attempt.exchange_id != claim.message.exchange_id
        or attempt.request_record_id != claim.message.request_record_id
        or attempt.close_reason not in ("SCOPE_STOPPED", "LEASE_EXPIRED", "LEASE_RELEASED")
        or attempt.response_state is not None
    ):
        return
    if attempt.attempt_state == "DELIVERY_UNKNOWN":
        attempt.attempt_state = "NOT_SENT"
        attempt.failure_kind = failure_kind
        attempt.late = True
        attempt.revision += 1


def _legacy_attempt_opening(attempt):
    gate = aliased(IntegrationRecord)
    return select(gate.id).where(
        gate.id == attempt.id,
        gate.exchange_id == attempt.exchange_id,
        gate.attempt_id == attempt.lease_id,
        gate.parent_record_id == attempt.request_record_id,
        gate.record_kind == "SEND_ATTEMPT_OPENED",
        gate.direction == "OUTBOUND",
        gate.occurred_at == attempt.started_at,
    )


def legacy_attempt_responses(attempt):
    record = IntegrationRecord
    return select(record.id, record.http_status, record.protocol_verdict).where(
        _legacy_attempt_opening(attempt).exists(),
        record.id == attempt.response_record_id,
        record.exchange_id == attempt.exchange_id,
        record.attempt_id == attempt.lease_id,
        record.record_kind == "HTTP_RESPONSE_OBSERVED",
        record.direction == "INBOUND",
        record.network_state == DeliveryState.HTTP_RESPONSE_COMPLETE,
        record.body_record_id.is_(None),
        attempt.response_state == "RESPONSE_OBSERVED",
    )


def verified_attempt_response(body, status, verdict, message, adapter):
    if body is None or body.raw_body is None or body.body_state not in {"FULL", "LEGACY_REENCODED"}:
        return None
    return verified_response_wire(body.raw_body, status, verdict, message, adapter)


def verified_response_wire(wire, status, verdict, message, adapter):
    if wire is None:
        return None
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, status, (), wire)
    try:
        return wire if adapter.classify_response(message, response).kind == verdict else None
    except ValueError:
        return None


async def reset_retry_budget(session, *, message_id):
    attempt = ReliableDeliveryAttempt
    await session.execute(
        update(attempt)
        .where(attempt.message_id == message_id, attempt.counts_toward_retry_budget.is_(True))
        .values(counts_toward_retry_budget=False)
    )


async def dispatch_attempt_summary(session, *, claim, within_budget=False) -> tuple[int, int, int]:
    attempt = ReliableDeliveryAttempt
    period = (attempt.counts_toward_retry_budget.is_(True),) if within_budget else ()
    exact = (
        attempt.exchange_id == claim.message.exchange_id,
        attempt.request_record_id == claim.message.request_record_id,
        attempt.finished_at.is_not(None),
    )
    observed = (
        attempt.response_state == "RESPONSE_OBSERVED",
        attempt.response_record_id.is_not(None),
    )
    declared_refusal = attempt.response_verdict == "NOT_RECEIVED"
    formal_wms = attempt.protocol.in_(WMS_PROTOCOLS)
    use_runtime = enabled()
    possible_refusal = (
        declared_refusal
        if use_runtime
        else or_(declared_refusal, attempt.response_verdict.is_(None))
    )
    counts = await session.execute(
        select(
            func.count(),
            func.count().filter(
                *exact,
                attempt.attempt_state == "NOT_SENT",
                attempt.response_state.is_(None),
                attempt.response_record_id.is_(None),
            ),
            func.count().filter(
                *exact,
                *observed,
                ~formal_wms,
                declared_refusal,
            ),
            func.count().filter(*exact, *observed, formal_wms, possible_refusal),
        ).where(attempt.message_id == claim.message_id, *period)
    )
    total, unsent, declined, wms_replies = counts.one()
    if wms_replies:
        declined += await _verified_decline_count(
            session,
            claim,
            (*exact, *period, *observed),
            possible_refusal,
            use_runtime=use_runtime,
        )
    return total, unsent, declined


async def legacy_dispatch_gate_count(session, *, claim, row) -> int | None:
    attempt = ReliableDeliveryAttempt
    opening = (
        attempt.request_wire == claim.message.request_raw
        if enabled()
        else _legacy_attempt_opening(attempt).exists()
    )
    openings, first = (
        await session.execute(
            select(
                func.count(), func.count().filter(attempt.started_at == row.dispatch_started_at)
            ).where(
                attempt.message_id == row.id,
                attempt.exchange_id == row.exchange_id,
                attempt.request_record_id == row.request_record_id,
                attempt.operation == row.operation,
                attempt.operation_id == row.operation_id,
                attempt.protocol == row.protocol,
                attempt.lease_id != row.lease_id,
                attempt.finished_at <= row.last_attempt_at,
                attempt.close_reason.in_(("FINISHED", "LEASE_EXPIRED", "LEASE_RELEASED")),
                opening,
            )
        )
    ).one()
    if not first or openings > row.attempts:
        return None
    if openings < row.attempts and not enabled():
        # Older recovery can count an unstarted lease. A retained opening without
        # its matching attempt is evidence of a gap.
        journal_openings = await session.scalar(
            select(func.count()).where(*_legacy_dispatch_openings(row))
        )
        if journal_openings != openings:
            return None
    return openings


def _legacy_dispatch_openings(row):
    record = IntegrationRecord
    return (
        record.exchange_id == row.exchange_id,
        record.parent_record_id == row.request_record_id,
        record.record_kind == "SEND_ATTEMPT_OPENED",
        record.direction == "OUTBOUND",
    )


async def dispatch_history_incomplete(session, *, claim, row, attempt_count) -> bool:
    if not enabled():
        return False
    open_lease = row.status == "SENDING" and row.lease_not_started is not True
    stopped_lease = row.scope_stopped_lease_id is not None
    if attempt_count < row.attempts - row.undispatched_attempts + open_lease + stopped_lease:
        return True
    if row.status == "ABANDONED" and row.lease_not_started is not True:
        attempt = ReliableDeliveryAttempt
        latest_gate = stopped_dispatch_gate(
            attempt, row.scope_stopped_lease_id, row.last_attempt_at
        )
        if not await session.scalar(
            select(attempt.id)
            .where(
                attempt.message_id == row.id,
                attempt.exchange_id == row.exchange_id,
                attempt.request_record_id == row.request_record_id,
                latest_gate,
            )
            .limit(1)
        ):
            return True
    if not await _has_legacy_journal(session):
        return False
    record = IntegrationRecord
    attempt = ReliableDeliveryAttempt
    matching_attempt = (
        select(attempt.id)
        .where(
            attempt.id == record.id,
            attempt.message_id == row.id,
            attempt.lease_id == record.attempt_id,
            attempt.exchange_id == record.exchange_id,
            attempt.request_record_id == record.parent_record_id,
            attempt.started_at == record.occurred_at,
            attempt.operation == row.operation,
            attempt.operation_id == row.operation_id,
            attempt.protocol == row.protocol,
            attempt.request_wire == claim.message.request_raw,
        )
        .exists()
    )
    unmatched = await session.scalar(
        select(record.id).where(*_legacy_dispatch_openings(row), ~matching_attempt).limit(1)
    )
    return unmatched is not None


def stopped_dispatch_gate(attempt, stopped_lease_id, last_attempt_at):
    if stopped_lease_id is not None:
        return (attempt.lease_id == stopped_lease_id) & (attempt.close_reason == "SCOPE_STOPPED")
    if last_attempt_at is None:
        return false()
    # A pre-upgrade stop did not keep the lease ID. Require evidence for the latest claim.
    return (attempt.started_at >= last_attempt_at) & (
        (attempt.close_reason == "SCOPE_STOPPED") | (attempt.finished_at > last_attempt_at)
    )


async def _has_legacy_journal(session):
    def exists(connection):
        table = IntegrationRecord.__table__
        schema_map = connection.get_execution_options().get("schema_translate_map") or {}
        return inspect(connection).has_table(
            table.name, schema=schema_map.get(table.schema, table.schema)
        )

    return await (await session.connection()).run_sync(exists)


async def _verified_decline_count(session, claim, exact, possible_refusal, *, use_runtime):
    from app.integrations import history
    from app.integrations.wms_request.adapter import ADAPTERS as wms_adapters
    from app.integrations.wms_transport.adapter import ADAPTERS as transport_adapters

    message = claim.message
    adapters = wms_adapters | transport_adapters
    protocol = (
        select(ReliableMessage.protocol)
        .where(ReliableMessage.id == claim.message_id)
        .scalar_subquery()
    )
    attempt = ReliableDeliveryAttempt
    matches = (
        *exact,
        possible_refusal,
        attempt.message_id == claim.message_id,
        attempt.operation == message.operation,
        attempt.operation_id == message.operation_id,
        attempt.business_key.is_not_distinct_from(message.business_key),
        attempt.protocol == protocol,
        attempt.protocol.in_(tuple(adapters)),
    )
    if use_runtime:
        replies = await session.execute(
            select(attempt.response_wire, attempt.response_http_status, attempt.protocol).where(
                *matches, attempt.request_wire == message.request_raw
            )
        )
        return sum(
            verified_response_wire(wire, status, "NOT_RECEIVED", message, adapters[name])
            is not None
            for wire, status, name in replies
        )
    responses = (
        await session.execute(
            legacy_attempt_responses(attempt)
            .add_columns(attempt.protocol)
            .where(
                *matches,
                or_(attempt.request_wire.is_(None), attempt.request_wire == message.request_raw),
                IntegrationRecord.protocol_verdict == "NOT_RECEIVED",
            )
        )
    ).all()
    try:
        bodies = await history.read_bodies(
            session, record_ids=tuple(reply.id for reply in responses)
        )
    except history.EvidenceIntegrityError:
        return 0
    return sum(
        verified_attempt_response(
            bodies.get(reply.id),
            reply.http_status,
            reply.protocol_verdict,
            message,
            adapters[reply.protocol],
        )
        is not None
        for reply in responses
    )


async def note_response(session, *, claim, outcome, record_id, at, late=False):
    message_id = getattr(claim, "message_id", None)
    if message_id is None:
        return
    attempt = await session.get(
        ReliableDeliveryAttempt, (message_id, claim.lease_id), with_for_update=True
    )
    if attempt is None:
        return
    if attempt.response_record_id is None or (
        enabled()
        and attempt.response_state == "RESPONSE_INCOMPLETE"
        and outcome.result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
    ):
        attempt.response_record_id = record_id
        if enabled():
            attempt.response_wire = outcome.result.body
            attempt.response_http_status = outcome.result.status_code
        attempt.response_verdict = outcome.verdict.kind if outcome.verdict else None
        attempt.response_state = (
            "RESPONSE_OBSERVED"
            if outcome.result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
            else "RESPONSE_INCOMPLETE"
        )
        attempt.response_headers = headers(outcome.result.headers, RESPONSE_HEADERS)
        attempt.response_observed_at = outcome.result.response_observed_at
        attempt.response_completed_at = outcome.result.completed_at or at
        attempt.revision += 1
        attempt.late = late


def _values(attempt, request, response, reply):
    table, kind = OUTBOUND[attempt.protocol]
    sent = json_object(request.raw_body) or {} if request and request.raw_body is not None else {}
    wire = response.raw_body if response else None
    received = json_object(wire) or {} if wire is not None else {}
    completed = attempt.response_completed_at
    late = (
        completed
        if attempt.late
        or (
            completed is not None
            and attempt.finished_at is not None
            and completed > attempt.finished_at
        )
        else None
    )
    values = dict(
        id=attempt.id,
        initiator="WES",
        interaction_kind=kind,
        traffic_source=attempt.traffic_source,
        message_id=attempt.message_id,
        lease_id=attempt.lease_id,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        response_observed_at=attempt.response_observed_at,
        late_response_at=late,
        http_method=attempt.http_method,
        endpoint_path=attempt.endpoint_path,
        peer_endpoint=attempt.peer_endpoint,
        trace_id=attempt.trace_id,
        request_headers=attempt.request_headers,
        response_headers=attempt.response_headers,
        request_query=attempt.request_query,
        attempt_state=attempt.attempt_state,
        close_reason=attempt.close_reason,
        failure_kind=attempt.failure_kind,
        http_status=reply.http_status if reply else None,
        protocol_verdict=reply.protocol_verdict if reply else None,
        protocol_code=safe_text(received.get("message" if table is EcsHttpExchange else "code")),
        source_revision=attempt.revision,
    )
    if table is EcsHttpExchange:
        values.update(
            command_code=attempt.operation_id,
            device_code=safe_text(sent.get("device_code")),
            task_type=safe_text(sent.get("task_type")),
        )
    elif table is WmsHttpExchange:
        values.update(
            operation_id=operation_uuid(attempt.operation_id), operation=attempt.operation
        )
    else:
        values.update(transport_task_id=attempt.business_key, task_match_state="MATCHED")
    data = received.get("data")
    if kind == "DECISION" and isinstance(data, dict):
        values["reported_result"] = safe_text(data.get("result"))
    values.update(
        body_fields(
            "request",
            request.raw_body if request else None,
            request.body_state if request else "UNAVAILABLE",
            sent.get("params" if table is EcsHttpExchange else "data"),
        )
    )
    values.update(
        body_fields("response", wire, response.body_state if response else "UNAVAILABLE", data)
    )
    return table, values


async def reconcile_outbound(factory, *, now=None, retention_days=90, limit=100):
    """Restart-safe projection. Runtime revisions remain pending when this transaction fails."""
    from app.integrations.history import read_bodies
    from app.models import IntegrationRecord
    from app.reliable.lifecycle import transaction_limits

    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    async with factory.begin() as session:
        await transaction_limits(session, 1)
        queue = http_projection_pending
        source = ReliableDeliveryAttempt
        attempts = (
            await session.scalars(
                select(source)
                .join_from(queue, source, queue.c.attempt_id == source.id)
                .where(source.started_at >= cutoff)
                .order_by(queue.c.attempt_id)
                .limit(limit)
                .with_for_update(of=source, skip_locked=True)
            )
        ).all()
        ids = tuple(
            {
                identity
                for row in attempts
                for identity in (row.request_record_id, row.response_record_id)
                if identity
            }
        )
        postgres = session.get_bind().dialect.name == "postgresql"
        insert = pg_insert if postgres else sqlite_insert
        bodies, replies = {}, {}
        if not enabled():
            bodies = await read_bodies(session, record_ids=ids)
            response_ids = tuple(
                row.response_record_id for row in attempts if row.response_record_id
            )
            replies = (
                {
                    reply.id: reply
                    for reply in await session.scalars(
                        select(IntegrationRecord).where(IntegrationRecord.id.in_(response_ids))
                    )
                }
                if response_ids
                else {}
            )
        for attempt in attempts:
            if enabled():
                from types import SimpleNamespace

                request_body = SimpleNamespace(raw_body=attempt.request_wire, body_state="FULL")
                response_body = SimpleNamespace(
                    raw_body=attempt.response_wire,
                    body_state="UNAVAILABLE"
                    if attempt.response_wire is None
                    else ("FULL" if attempt.response_state == "RESPONSE_OBSERVED" else "PREVIEW"),
                )
                reply = SimpleNamespace(
                    http_status=attempt.response_http_status,
                    protocol_verdict=attempt.response_verdict,
                )
            else:
                request_body = bodies.get(attempt.request_record_id)
                response_body = bodies.get(attempt.response_record_id)
                reply = replies.get(attempt.response_record_id)
            model, values = _values(attempt, request_body, response_body, reply)
            table = model.__table__
            statement = insert(table).values(**values)
            updates = {key: statement.excluded[key] for key in values if key != "id"}
            for side in ("request", "response"):
                state = table.c[f"{side}_wire_state"]
                excluded_state = statement.excluded[f"{side}_wire_state"]
                keep = (state == "PURGED") | ((state == "FULL") & (excluded_state != "FULL"))
                for suffix in ("wire", "wire_state", "wire_sha256", "wire_bytes", "payload"):
                    key = f"{side}_{suffix}"
                    updates[key] = case((keep, table.c[key]), else_=updates[key])
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[table.c.id],
                    set_=updates,
                    where=table.c.source_revision < statement.excluded.source_revision,
                )
            )
        if attempts:
            if postgres:
                projected_ids = [row.id for row in attempts]
            else:
                # SQLite can commit a newer revision between the read and first write.
                projected_ids = select(source.id).where(
                    tuple_(source.id, source.revision).in_(
                        [(row.id, row.revision) for row in attempts]
                    )
                )
            await session.execute(delete(queue).where(queue.c.attempt_id.in_(projected_ids)))
        return len(attempts)


async def save_observation(factory, model, values):
    """Diagnostics never participate in the protocol transaction."""
    import asyncio

    from app.diagnostics import log_exception

    try:
        async with asyncio.timeout(1), factory.begin() as session:
            table = model.__table__
            insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
            await session.execute(
                insert(table).values(**values).on_conflict_do_nothing(index_elements=[table.c.id])
            )
    except Exception as error:
        log_exception(
            logger,
            "HTTP exchange capture failed",
            error,
            component="http_exchange",
            phase="capture",
            context={
                "exchange_id": str(values["id"]),
                "trace_id": values.get("trace_id"),
                "source": model.__tablename__,
            },
        )


async def capture_callback(
    factory,
    *,
    identity,
    source,
    request,
    started_at,
    raw,
    limit,
    result,
    commit_unknown=False,
    traffic_source="PEER",
):
    from app.core.request_context import get_trace_id

    finished_at = datetime.now(UTC)
    parsed = json_object(raw) or {} if raw is not None and len(raw) <= limit else {}
    reply = result.body or {}
    operation = safe_text(parsed.get("operation"), 256)
    data = parsed.get("data")
    data_object = data if isinstance(data, dict) else {}
    transport = {
        "transport.task.resulted@v1": "RESULT_CALLBACK",
        "transport.task.member_position_changed@v1": "POSITION_CALLBACK",
    }
    reason = result.diagnostic_reason
    code = safe_text(reply.get("code") if source == "wms" else reply.get("message"))
    outcome = result.outcome
    if source in {"ecs_event", "ecs_request", "ecs_result"}:
        model = EcsHttpExchange
        details = dict(
            interaction_kind=(
                "EVENT"
                if source == "ecs_event"
                else "REQUEST"
                if source == "ecs_request"
                else "RESULT"
            ),
            command_code=safe_text(parsed.get("command_code")),
            device_code=safe_text(parsed.get("device_code")),
            event_type=safe_text(
                parsed.get("request_type") if source == "ecs_request" else parsed.get("event_type")
            ),
            reported_result=safe_text(parsed.get("result")),
        )
    elif operation in transport:
        model = TransportHttpExchange
        revision = data_object.get("outcome_revision")
        details = dict(
            interaction_kind=transport[operation],
            transport_task_id=safe_text(data_object.get("transport_task_id"), 256),
            task_match_state=(
                "UNMATCHED"
                if reason == "UNKNOWN_TASK"
                else "MATCHED"
                if result.status_code in {200, 202} and result.receipt_id is not None
                else "UNKNOWN"
            ),
            outcome_revision=revision if type(revision) is int and 0 < revision < 2**63 else None,
            reported_result=safe_text(data_object.get("status")),
        )
    else:
        model = WmsHttpExchange
        details = dict(
            interaction_kind="CALLBACK",
            operation=operation,
            operation_id=operation_uuid(parsed.get("operation_id")),
        )
    handling = (
        "CONFLICT"
        if outcome in {"conflict", "idempotency_conflict", "result_before_dispatch"}
        else "REPLAYED"
        if outcome == "replay" or (source != "wms" and result.pure_replay)
        else "DUPLICATE"
        if source == "wms" and code == "DUPLICATE"
        else "DEFERRED"
        if result.deferred
        else "ACCEPTED"
        if result.status_code < 400
        else "REJECTED"
    )
    reply_data = reply.get("data")
    values = dict(
        id=identity,
        initiator="PEER",
        traffic_source=traffic_source,
        started_at=started_at,
        finished_at=finished_at,
        http_method=request.method,
        endpoint_path=request.url.path,
        trace_id=None if get_trace_id() == "-" else get_trace_id(),
        request_headers=headers(request.headers.items(), REQUEST_HEADERS),
        response_headers={"content-type": ["application/json"]}
        if result.body is not None
        else None,
        attempt_state="RESPONSE_PREPARED",
        http_status=result.status_code,
        protocol_code=code,
        handling_outcome=handling,
        receipt_id=result.receipt_id,
        replayed_from_id=result.replayed_from_id if handling == "REPLAYED" else None,
        reason_code="COMMIT_UNKNOWN"
        if commit_unknown
        else reason
        or (safe_text(reply_data.get("reason_code")) if isinstance(reply_data, dict) else None)
        or (code if result.status_code >= 400 else None),
        failure_kind="BODY_TOO_LARGE"
        if raw is not None and len(raw) > limit
        else "BODY_READ_FAILED"
        if raw is None
        else None,
        **details,
    )
    values.update(
        body_fields(
            "request",
            raw,
            "UNAVAILABLE" if raw is None else "PREVIEW" if len(raw) > limit else "FULL",
            data,
        )
    )
    values.update(body_fields("response", result.prepared_body, "FULL", reply_data))
    await save_observation(factory, model, values)


async def capture_status(factory, *, origin, request, started_at, result, reason):
    from app.core.identifiers import uuid7
    from app.core.request_context import get_trace_id

    raw = result.body if result else None
    payload = json_object(raw) or {} if raw is not None else {}
    device_code = dict(request.query).get("device_code")
    completed = result.completed_at if result else None
    values = dict(
        id=uuid7(started_at),
        initiator="WES",
        interaction_kind="STATUS",
        traffic_source="PEER",
        started_at=started_at,
        finished_at=completed or datetime.now(UTC),
        response_observed_at=result.response_observed_at if result else None,
        http_method=request.method,
        endpoint_path=request.path,
        peer_endpoint=origin,
        device_code=device_code,
        request_query={"device_code": device_code} if device_code else None,
        trace_id=None if get_trace_id() == "-" else get_trace_id(),
        response_headers=headers(result.headers, RESPONSE_HEADERS) if result else None,
        attempt_state=(
            "RESPONSE_OBSERVED"
            if result and result.delivery_state == DeliveryState.HTTP_RESPONSE_COMPLETE
            else "NOT_SENT"
            if result and result.delivery_state == DeliveryState.NOT_SENT
            else "RESPONSE_INCOMPLETE"
            if result and result.status_code is not None
            else "DELIVERY_UNKNOWN"
        ),
        http_status=result.status_code if result else None,
        reason_code=reason,
        failure_kind=result.failure_kind if result else None,
    )
    values.update(body_fields("request", request.body, "FULL", None))
    values.update(
        body_fields(
            "response", raw, "FULL" if raw is not None else "UNAVAILABLE", payload.get("devices")
        )
    )
    await save_observation(factory, EcsHttpExchange, values)


async def cleanup_exchanges(factory, days, *, now=None, limit=500):
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    attempt = ReliableDeliveryAttempt
    models = (WmsHttpExchange, EcsHttpExchange, TransportHttpExchange)
    while True:
        async with factory.begin() as session:
            ids = (
                await session.scalars(
                    select(attempt.id)
                    .where(attempt.started_at < cutoff, attempt.finished_at.is_not(None))
                    .order_by(attempt.id)
                    .limit(limit)
                    .with_for_update()
                )
            ).all()
            if not ids:
                break
            # Projection and cleanup take the attempt lock before touching the journal.
            for model in models:
                await session.execute(delete(model).where(model.id.in_(ids)))
            # SQLite connections do not necessarily enforce the foreign key cascade.
            await session.execute(
                delete(http_projection_pending).where(http_projection_pending.c.attempt_id.in_(ids))
            )
            await session.execute(delete(attempt).where(attempt.id.in_(ids)))
        if len(ids) < limit:
            break
    for model in models:
        while True:
            async with factory.begin() as session:
                batch = (
                    select(model.id)
                    .where(
                        model.started_at < cutoff,
                        model.finished_at.is_not(None),
                        model.message_id.is_(None),
                    )
                    .order_by(model.started_at, model.id)
                    .limit(limit)
                )
                result = await session.execute(delete(model).where(model.id.in_(batch)))
            if result.rowcount < limit:
                break
