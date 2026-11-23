"""Read-only original-message results; no delivery or business-completion side effects."""

from dataclasses import dataclass
from datetime import UTC

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.clock import unix_ms
from app.core.strict_json import json_object
from app.integrations import history
from app.integrations.http_exchanges import (
    legacy_attempt_responses,
    stopped_dispatch_gate,
    verified_attempt_response,
    verified_response_wire,
)
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
            attempts=ReliableMessage.attempts,
            undispatched_attempts=ReliableMessage.undispatched_attempts,
            pause_reason=ReliableMessage.pause_reason,
            dispatch_started_at=ReliableMessage.dispatch_started_at,
            dispatch_attempt_count=ReliableMessage.dispatch_attempt_count,
            last_attempt_at=ReliableMessage.last_attempt_at,
            lease_not_started=ReliableMessage.lease_not_started,
            scope_stopped_lease_id=ReliableMessage.scope_stopped_lease_id,
            protocol=ReliableMessage.protocol,
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
            observed = await _stopped_attempt(session, row, message, adapter)
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
        delivery_uncertain=row["delivery_uncertain"] or awaiting_stopped_response,
        fail_reason=row["fail_reason"],
        response_received_at_ms=unix_ms(received_at) if received_at is not None else None,
        late_response_record_id=late_response_record_id,
        awaiting_stopped_response=awaiting_stopped_response,
        **response,
    )


async def _stopped_attempt(session, row, message, adapter):
    attempt = ReliableDeliveryAttempt
    other = aliased(attempt)
    use_runtime = enabled()

    def matches_original(candidate):
        request = candidate.request_wire == row["request_raw"]
        if not use_runtime:
            request = or_(candidate.request_wire.is_(None), request)
        return and_(
            candidate.exchange_id == row["exchange_id"],
            candidate.request_record_id == row["request_record_id"],
            candidate.operation == row["operation"],
            candidate.operation_id == row["operation_id"],
            candidate.protocol == row["protocol"],
            request,
        )

    declined = other.response_verdict == "NOT_RECEIVED"
    if not use_runtime:
        legacy_responses = legacy_attempt_responses(other)
        declined = legacy_responses.where(
            IntegrationRecord.protocol_verdict == "NOT_RECEIVED"
        ).exists()
    observed_decline = (
        attempt.response_verdict == "NOT_RECEIVED"
        if use_runtime
        else legacy_attempt_responses(attempt)
        .where(IntegrationRecord.protocol_verdict == "NOT_RECEIVED")
        .exists()
    )

    non_delivery = and_(
        matches_original(other),
        other.finished_at.is_not(None),
        or_(
            and_(
                other.attempt_state == "NOT_SENT",
                other.response_state.is_(None),
                other.response_record_id.is_(None),
            ),
            and_(
                other.response_state == "RESPONSE_OBSERVED",
                other.response_record_id.is_not(None),
                declined,
            ),
        ),
    )
    competing = select(1).where(
        other.message_id == attempt.message_id,
        other.lease_id != attempt.lease_id,
        non_delivery.is_not(True),
    )
    recorded = other.message_id == row["id"]
    recorded_count = select(func.count()).where(recorded).scalar_subquery()
    gate_count = row["dispatch_attempt_count"]
    stopped_lease = row["scope_stopped_lease_id"]
    closed_count = row["attempts"] - row["undispatched_attempts"]
    if stopped_lease is not None:
        complete = recorded_count == closed_count + 1
    elif gate_count is not None:
        complete = recorded_count == gate_count
    else:
        first_gate = select(1).where(recorded, other.started_at == row["dispatch_started_at"])
        last_claim_at = row["last_attempt_at"]
        if last_claim_at is None:
            return None
        latest_gate = select(1).where(recorded, other.started_at >= last_claim_at)
        stopped_gate = latest_gate.where(other.close_reason == "SCOPE_STOPPED")
        completed_gate = latest_gate.where(other.finished_at > last_claim_at)
        # Only a gated lease closed by scope stop adds a row without incrementing attempts.
        complete = and_(
            first_gate.exists(),
            or_(
                and_(
                    recorded_count == closed_count + 1,
                    row["lease_not_started"] is False,
                    stopped_gate.exists(),
                ),
                and_(
                    recorded_count == closed_count,
                    ~stopped_gate.exists(),
                    or_(row["lease_not_started"] is True, completed_gate.exists()),
                ),
            ),
        )
    if use_runtime:
        complete = and_(complete, recorded_count >= closed_count)
    if stopped_lease is not None or use_runtime and row["lease_not_started"] is not True:
        complete = and_(
            complete,
            select(1)
            .where(
                recorded,
                matches_original(other),
                stopped_dispatch_gate(other, stopped_lease, row["last_attempt_at"]),
            )
            .exists(),
        )
    # A possible acceptance excludes every other candidate. Multiple candidates
    # are all refusals, so keep the stopped lease's reply when it is present.
    original = await session.scalar(
        select(attempt)
        .where(
            attempt.message_id == row["id"],
            matches_original(attempt),
            complete,
            ~competing.exists(),
            attempt.finished_at.is_not(None),
            or_(
                attempt.close_reason.in_(("SCOPE_STOPPED", "LEASE_EXPIRED", "LEASE_RELEASED")),
                and_(attempt.close_reason == "FINISHED", observed_decline),
            ),
            attempt.response_state == "RESPONSE_OBSERVED",
            attempt.response_record_id.is_not(None),
        )
        .order_by(
            (attempt.close_reason == "SCOPE_STOPPED").desc(),
            attempt.started_at.desc(),
            attempt.id.desc(),
        )
        .limit(1)
    )
    if original is None or adapter is None:
        return None
    record_id = original.response_record_id
    if use_runtime:
        replies = await session.execute(
            select(other.response_wire, other.response_http_status, other.response_verdict).where(
                recorded, other.response_state == "RESPONSE_OBSERVED"
            )
        )
        if any(
            verified_response_wire(wire, status, verdict, message, adapter) is None
            for wire, status, verdict in replies
        ):
            return None
        return (
            record_id,
            original.response_completed_at,
            original.response_http_status,
            original.response_wire,
            original.response_verdict,
        )
    responses = (await session.execute(legacy_responses.where(recorded))).all()
    try:
        bodies = await history.read_bodies(
            session, record_ids=tuple(reply.id for reply in responses)
        )
    except history.EvidenceIntegrityError:
        return None
    observed = None
    for reply_id, status, verdict in responses:
        wire = verified_attempt_response(bodies.get(reply_id), status, verdict, message, adapter)
        if wire is None:
            return None
        if reply_id == record_id:
            observed = (record_id, original.response_completed_at, status, wire, verdict)
    return observed
