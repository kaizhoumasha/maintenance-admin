"""Read-only original-message results; no delivery or business-completion side effects."""

from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.strict_json import json_object
from app.integrations import history
from app.integrations.runtime_authority import enabled
from app.integrations.wms_envelope import (
    outgoing_message,
    response_projection,
    response_row,
    stored_response,
)
from app.models import IntegrationRecord, ReliableDeliveryAttempt, ReliableMessage

from .adapter import ADAPTERS, PROTOCOL_OF
from .operation import lookup


@dataclass(frozen=True, slots=True, kw_only=True)
class WmsRequestResult:
    message_id: int
    operation: str
    operation_id: str
    availability: str
    target: str | None = None
    delivery_status: str | None = None
    delivery_terminal: bool | None = None
    delivery_uncertain: bool | None = None
    fail_reason: str | None = None
    response_state: str = "UNAVAILABLE"
    http_status: int | None = None
    response_code: str | None = None
    response_timestamp: int | None = None
    response_data: dict | None = None
    response_received_at_ms: int | None = None
    late_response_record_id: str | None = None
    awaiting_stopped_response: bool = False


async def get_result(
    session: AsyncSession,
    *,
    message_id: int,
    operation: str,
    operation_id: str,
    stopped_reason: str | None = None,
) -> WmsRequestResult:
    identity = dict(message_id=message_id, operation=operation, operation_id=operation_id)
    clause, columns = response_projection(ReliableMessage.__table__)
    if stopped_reason is not None:
        columns.update(
            pause_reason=ReliableMessage.pause_reason,
            dispatch_started_at=ReliableMessage.dispatch_started_at,
        )
    with session.no_autoflush:
        row = (
            (
                await session.execute(
                    select(*columns.values())
                    .select_from(clause)
                    .where(ReliableMessage.id == message_id)
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return WmsRequestResult(**identity, availability="NOT_FOUND")
    if row["operation"] != operation or row["operation_id"] != operation_id:
        return WmsRequestResult(**identity, availability="IDENTITY_MISMATCH")
    row = response_row(row)
    raw = json_object(row["request_raw"]) if row["request_raw"] is not None else None
    if not raw or raw.get("operation") != operation or raw.get("operation_id") != operation_id:
        return WmsRequestResult(**identity, availability="INVALID")
    registered = lookup(operation)
    adapter = ADAPTERS[PROTOCOL_OF[registered.endpoint]] if registered else None
    message = outgoing_message(row)
    response = stored_response(message, row, adapter)
    received_at = row["terminal_occurred_at"]
    late_response_record_id = None
    awaiting_stopped_response = False
    if (
        stopped_reason is not None
        and row["status"] == "ABANDONED"
        and row["pause_reason"] == stopped_reason
        and row["dispatch_started_at"] is not None
    ):
        with session.no_autoflush:
            observed = await _stopped_attempt(session, row)
        awaiting_stopped_response = observed is None
        if observed is not None:
            record_id, received_at, status, wire, verdict = observed
            late_response_record_id = str(record_id)
            if verdict == "DELIVERED":
                # Read the original reply without changing the stopped delivery's state.
                response = stored_response(
                    message,
                    dict(
                        row,
                        status="DONE",
                        last_http_status=status,
                        last_verdict=verdict,
                        last_failure_kind=None,
                        response_raw=wire,
                    ),
                    adapter,
                )
    if received_at is not None and received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    return WmsRequestResult(
        **identity,
        availability="FOUND",
        target=row["target"],
        delivery_status=row["status"],
        delivery_terminal=row["status"] in {"DONE", "FAILED", "EXPIRED", "ABANDONED"},
        delivery_uncertain=row["delivery_uncertain"],
        fail_reason=row["fail_reason"],
        response_received_at_ms=unix_ms(received_at) if received_at is not None else None,
        late_response_record_id=late_response_record_id,
        awaiting_stopped_response=awaiting_stopped_response,
        **response,
    )


async def _stopped_attempt(session, row):
    attempt = ReliableDeliveryAttempt
    attempts = list(
        await session.scalars(
            select(attempt)
            .where(
                attempt.message_id == row["id"],
                attempt.exchange_id == row["exchange_id"],
                attempt.request_record_id == row["request_record_id"],
                attempt.operation == row["operation"],
                attempt.operation_id == row["operation_id"],
                attempt.peer_endpoint == row["target"],
                attempt.close_reason == "SCOPE_STOPPED",
                attempt.response_state == "RESPONSE_OBSERVED",
                attempt.response_record_id.is_not(None),
            )
            .limit(2)
        )
    )
    if len(attempts) != 1:
        return None
    original = attempts[0]
    record_id = original.response_record_id
    if enabled():
        return (
            record_id,
            original.response_completed_at,
            original.response_http_status,
            original.response_wire,
            original.response_verdict,
        )
    record = await session.get(IntegrationRecord, record_id)
    if (
        record is None
        or record.exchange_id != original.exchange_id
        or record.attempt_id != original.lease_id
        or record.record_kind != "HTTP_RESPONSE_OBSERVED"
    ):
        return None
    body = (await history.read_bodies(session, record_ids=(record_id,))).get(record_id)
    wire = body.raw_body if body and body.body_state in {"FULL", "LEGACY_REENCODED"} else None
    return (
        record_id,
        original.response_completed_at,
        record.http_status,
        wire,
        record.protocol_verdict,
    )
