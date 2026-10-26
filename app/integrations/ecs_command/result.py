from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.strict_json import canonical_digest, json_object
from app.integrations.callback_receipts import insert_receipt
from app.integrations.ecs_callback.envelope import issues_from
from app.integrations.ecs_callback.receiver import EcsResponse
from app.models import EcsCommand, EcsResultEvidence, EcsSendEvidence, ReliableMessage
from app.reliable import Outbox
from app.reliable.lifecycle import transaction_limits

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


def _block_for_reconciliation(command: EcsCommand, reason: str) -> None:
    command.status = "RECONCILING"
    command.blocked = True
    command.needs_reconciliation = True
    command.hold_reason = reason


async def receive_ecs_result(session: AsyncSession, raw: bytes, service) -> EcsResponse:
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
        select(EcsCommand)
        .where(EcsCommand.command_code == envelope.command_code)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if command is None or command.device_code != envelope.device_code:
        return reply(404, "COMMAND_NOT_FOUND")
    evidence = await session.scalar(
        select(EcsResultEvidence).where(
            EcsResultEvidence.command_code == command.command_code,
            EcsResultEvidence.digest == digest,
        )
    )
    if evidence:
        if evidence.core != core:
            raise ValueError("result digest collision")
        return EcsResponse(evidence.status_code, evidence.response, "duplicate")
    models = service.annexes.get((command.route["annex_id"], command.route["annex_version"]))
    if models is None:
        raise RuntimeError("original ECS annex unavailable")
    try:
        models.result_model.model_validate(envelope.data)
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
    attempts = list(
        await session.scalars(
            select(EcsSendEvidence).where(EcsSendEvidence.command_code == command.command_code)
        )
    )
    possible = any(a.outcome in ("OPEN_ATTEMPT", "UNKNOWN", "ACK") for a in attempts)
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

    outbox = Outbox({}, {}, lifecycles={PROTOCOL: EcsLifecycle(service)})
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
    session.add(
        EcsResultEvidence(
            command_code=command.command_code,
            digest=digest,
            core=core,
            status_code=response.status_code,
            response=response.body,
            applied=applied,
            created_at=now,
        )
    )
    await insert_receipt(
        session,
        operation="ecs.result",
        operation_id=digest,
        raw_digest=raw_digest,
        effective_digest=None,
        compare_mode="RAW",
        status_code=response.status_code,
        body=response.body,
        resp_timestamp=unix_ms(now),
        created_at=now,
    )
    return response
