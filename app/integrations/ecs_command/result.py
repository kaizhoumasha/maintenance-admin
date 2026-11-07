from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest, json_object
from app.integrations import history as history_records
from app.integrations.callback_receipts import insert_receipt
from app.integrations.ecs_callback.envelope import issues_from
from app.integrations.ecs_callback.receiver import EcsResponse
from app.models import IntegrationExchange, IntegrationRecord, ReliableMessage
from app.reliable import Outbox
from app.reliable.lifecycle import transaction_limits
from app.transactions import after_commit

from .contracts import ResultEnvelope
from .protocol import OPERATION

ACK = {"code": 200, "message": "ACK"}


def reply(status, message):
    return EcsResponse(status, {"code": status, "message": message}, message.lower())


def invalid(error=None, model=ResultEnvelope, prefix=""):
    issues = (
        issues_from(error, model=model, prefix=prefix)
        if error
        else [{"field": "$", "code": "INVALID_JSON"}]
    )
    if error:
        for issue in issues:
            if issue["field"].endswith(".*"):
                issue["field"] = issue["field"][:-1] + "<extra>"
                issue["code"] = "EXTRA_FORBIDDEN"
    return EcsResponse(
        400,
        {"code": 400, "message": "INVALID_ENVELOPE", "error_detail": {"issues": issues}},
        "invalid",
    )


def _block_for_reconciliation(command: IntegrationExchange, reason: str) -> None:
    command.status = "RECONCILING"
    command.blocked = True
    command.needs_reconciliation = True
    command.hold_reason = reason


async def receive_ecs_result(
    session: AsyncSession, raw: bytes, service, *, on_result=None, attempt_id: str | None = None
) -> EcsResponse:
    attempt_id = attempt_id or str(uuid7())
    if len(raw) > 262144:
        return reply(413, "PAYLOAD_TOO_LARGE")
    parsed = json_object(raw)
    if parsed is None:
        return invalid()
    try:
        raw_digest = canonical_digest(parsed)
        envelope = ResultEnvelope.model_validate(parsed)
        core = envelope.model_dump()
        digest = canonical_digest(core)
    except ValidationError as error:
        return invalid(error)
    except (ValueError, RecursionError):
        return invalid()
    await transaction_limits(session)
    command = await session.scalar(
        select(IntegrationExchange)
        .where(
            IntegrationExchange.role == "ECS_COMMAND",
            IntegrationExchange.command_code == envelope.command_code,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if command is None or command.device_code != envelope.device_code:
        return reply(404, "COMMAND_NOT_FOUND")
    evidence = await session.scalar(
        select(IntegrationRecord).where(
            IntegrationRecord.exchange_id == command.id,
            IntegrationRecord.record_kind == "RESULT_EVIDENCE",
            IntegrationRecord.semantic_digest == digest,
        )
    )
    if evidence:
        if evidence.effective_payload != core:
            raise ValueError("result digest collision")
        bodies = await history_records.read_bodies(
            session, record_ids=(evidence.response_record_id,)
        )
        saved = bodies.get(evidence.response_record_id)
        if saved is None or saved.raw_body is None or json_object(saved.raw_body) is None:
            raise history_records.EvidenceIntegrityError("original result reply unavailable")
        return EcsResponse(
            evidence.http_status,
            json_object(saved.raw_body),
            "duplicate",
            digest=digest,
            pure_replay=True,
            exchange_id=command.id,
            attempt_id=attempt_id,
            prepared_body=saved.raw_body,
        )
    models = service.annexes.get((command.route["annex_id"], command.route["annex_version"]))
    if models is None:
        raise RuntimeError("original ECS annex unavailable")
    try:
        models.result_model.model_validate(envelope.data, context={"result": envelope.result})
    except ValidationError as error:
        return invalid(error, models.result_model, "data")
    if envelope.error_detail is not None:
        try:
            models.error_model.model_validate(envelope.error_detail.model_dump())
        except ValidationError as error:
            return invalid(error, models.error_model, "error_detail")
    message = await session.scalar(
        select(ReliableMessage)
        .where(
            ReliableMessage.target == command.target,
            ReliableMessage.operation == OPERATION,
            ReliableMessage.operation_id == command.command_code,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    from .lifecycle import get_attempt_conclusions

    attempts = await get_attempt_conclusions(session, exchange_id=command.id)
    possible = any(outcome in ("OPEN_ATTEMPT", "UNKNOWN", "ACK") for outcome in attempts.values())
    resolvable_pauses = {"DELIVERY_UNKNOWN", "LEASE_INTERRUPTED"}
    if command.route["route_id"].startswith("device.route.") and command.route[
        "version"
    ].startswith("2:"):
        resolvable_pauses.add("AUTHORIZATION_REVOKED")
    applied = False
    if command.applied_result_id:
        response = reply(409, "IDEMPOTENCY_CONFLICT")
        command.needs_reconciliation = True
    elif command.blocked:
        response = reply(409, "IDEMPOTENCY_CONFLICT")
    elif message and (
        (message.status == "HELD" and message.pause_reason not in resolvable_pauses)
        or message.status in ("FAILED", "EXPIRED")
    ):
        response = reply(409, "IDEMPOTENCY_CONFLICT")
        _block_for_reconciliation(command, message.pause_reason or "IDENTITY_CONFLICT")
    elif command.dispatch_started_at is None:
        response = reply(409, "RESULT_BEFORE_DISPATCH")
        _block_for_reconciliation(command, "EARLY_RESULT_LOCK")
    elif not possible or command.status == "REJECTED":
        response = reply(409, "IDEMPOTENCY_CONFLICT")
        _block_for_reconciliation(command, "IDENTITY_CONFLICT")
    elif command.hold_reason and command.hold_reason not in resolvable_pauses:
        response = reply(409, "IDEMPOTENCY_CONFLICT")
        command.needs_reconciliation = True
    else:
        response = EcsResponse(200, dict(ACK), "received")
        command.status = "SUCCEEDED" if envelope.result == "SUCCESS" else "FAILED"
        command.applied_result_id = digest
        command.hold_reason = None
        applied = True
    from .lifecycle import EcsLifecycle
    from .protocol import PROTOCOL

    outbox = Outbox({}, {}, lifecycles={PROTOCOL: EcsLifecycle(service)}, records=history_records)
    if applied:
        await outbox.settle(session, command.target, OPERATION, command.command_code)
        command.delivery_status = "DONE"
    elif (
        command.blocked and message and message.status in ("PENDING", "SENDING", "STALLED", "HELD")
    ):
        await outbox.hold(
            session,
            command.target,
            OPERATION,
            command.command_code,
            reason=command.hold_reason or "IDENTITY_CONFLICT",
        )
        command.delivery_status = "HELD"
    now = datetime.now(UTC)
    response_id = await history_records.write_record(
        session,
        draft=history_records.RecordDraft(
            exchange_id=command.id,
            record_kind="RESPONSE_PREPARED",
            event_key=f"RESULT_REPLY:{digest}",
            direction="OUTBOUND",
            http_status=response.status_code,
            raw_body=response.prepared_body,
            attempt_id=attempt_id,
            occurred_at=now,
        ),
    )
    evidence_id = await history_records.write_record(
        session,
        draft=history_records.RecordDraft(
            exchange_id=command.id,
            record_kind="RESULT_EVIDENCE",
            event_key=f"RESULT:{digest}",
            direction="INBOUND",
            semantic_digest=digest,
            effective_payload=core,
            http_status=response.status_code,
            response_record_id=response_id,
            applied=applied,
            raw_body=raw,
            attempt_id=attempt_id,
            business_result=envelope.result,
            occurred_at=now,
        ),
    )
    receipt_id = await insert_receipt(
        session,
        operation="ecs.result",
        operation_id=digest,
        device_code=envelope.device_code,
        raw_digest=raw_digest,
        effective_digest=None,
        compare_mode="RAW",
        status_code=response.status_code,
        body=response.body,
        resp_timestamp=unix_ms(now),
        created_at=now,
        raw_body=raw,
        prepared_body=response.prepared_body,
        attempt_id=attempt_id,
        peer_system="ECS",
        protocol="ecs-result",
        request_record_id=evidence_id,
        response_record_id=response_id,
    )
    if receipt_id is not None:
        await history_records.link_receipt(
            session,
            receipt_id=receipt_id,
            reference_kind="EXCHANGE",
            namespace="integration",
            value=str(command.id),
            relation_role="ORIGINAL_COMMAND",
            related_exchange_id=command.id,
            at=now,
        )
    if applied and on_result is not None:
        after_commit(session, partial(on_result, command.owner_domain))
    return replace(response, digest=digest, exchange_id=command.id, attempt_id=attempt_id)
