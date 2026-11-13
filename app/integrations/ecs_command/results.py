"""Read original commands without sender or route assembly."""

from sqlalchemy import select

from app.integrations.history import EvidenceIntegrityError
from app.integrations.runtime_authority import enabled
from app.models import (
    EcsCommand,
    EcsResultEvidence,
    IntegrationExchange,
    IntegrationRecord,
    ReliableMessage,
)


async def get_command(session, *, owner_domain: str, command_code: str):
    from .protocol import OPERATION
    from .service import CommandView

    model = EcsCommand if enabled() else IntegrationExchange
    root = model.__table__
    evidence = (EcsResultEvidence if model is EcsCommand else IntegrationRecord).__table__.alias(
        "applied_result"
    )
    link = (
        evidence.c.command_code == root.c.command_code
        if model is EcsCommand
        else (evidence.c.exchange_id == root.c.id) & (evidence.c.record_kind == "RESULT_EVIDENCE")
    )
    message = ReliableMessage.__table__
    with session.no_autoflush:
        row = (
            (
                await session.execute(
                    select(
                        root.c.command_code,
                        root.c.status,
                        root.c.delivery_status,
                        root.c.ack_record_id,
                        root.c.applied_result_id,
                        root.c.hold_reason,
                        root.c.needs_reconciliation,
                        root.c.task_type,
                        root.c.device_code,
                        evidence.c.effective_payload,
                        message.c.id.label("message_id"),
                    )
                    .select_from(
                        root.outerjoin(
                            evidence,
                            link & (evidence.c.semantic_digest == root.c.applied_result_id),
                        ).outerjoin(
                            message,
                            (message.c.exchange_id == root.c.id)
                            & (message.c.target == root.c.target)
                            & (message.c.operation == OPERATION)
                            & (message.c.operation_id == root.c.command_code),
                        )
                    )
                    .where(
                        root.c.owner_domain == owner_domain,
                        root.c.command_code == command_code,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return None
    if row["applied_result_id"] is not None and row["effective_payload"] is None:
        raise EvidenceIntegrityError("original applied result unavailable")
    return CommandView(
        row["command_code"],
        row["status"],
        row["delivery_status"],
        row["ack_record_id"] is not None,
        row["effective_payload"],
        row["hold_reason"],
        row["needs_reconciliation"],
        message_id=row["message_id"],
        task_type=row["task_type"],
        device_code=row["device_code"],
    )


async def is_delivery_proven_unaccepted(session, *, owner_domain: str, command_code: str) -> bool:
    """Read terminal nonacceptance under the caller's consistent transaction snapshot."""
    from .lifecycle import get_attempt_conclusions
    from .protocol import OPERATION

    root = EcsCommand if enabled() else IntegrationExchange
    message = ReliableMessage
    with session.no_autoflush:
        command = (
            await session.execute(
                select(root.id, root.dispatch_started_at)
                .join(
                    message,
                    (message.exchange_id == root.id)
                    & (message.target == root.target)
                    & (message.operation == OPERATION)
                    & (message.operation_id == root.command_code),
                )
                .where(
                    root.owner_domain == owner_domain,
                    root.command_code == command_code,
                    root.ack_record_id.is_(None),
                    root.applied_result_id.is_(None),
                    root.blocked.is_(False),
                    root.needs_reconciliation.is_(False),
                    message.status == root.delivery_status,
                    message.status.in_(("EXPIRED", "FAILED", "ABANDONED")),
                    message.delivery_uncertain.is_(False),
                )
            )
        ).one_or_none()
    if command is None:
        return False
    attempts = await get_attempt_conclusions(session, exchange_id=command.id, require_open=True)
    if command.dispatch_started_at is None:
        return not attempts
    return bool(attempts) and all(v in ("REJECTED", "NOT_RECEIVED") for v in attempts.values())


async def get_message_ids(session, *, owner_domain, command_codes, candidate_ids):
    """Read original frozen command/message associations, without result consumption."""
    from app.models import ReliableMessage

    from .protocol import OPERATION

    if not command_codes or not candidate_ids:
        return {}
    command = EcsCommand if enabled() else IntegrationExchange
    with session.no_autoflush:
        rows = await session.execute(
            select(command.command_code, ReliableMessage.id)
            .join(
                ReliableMessage,
                (ReliableMessage.target == command.target)
                & (ReliableMessage.operation == OPERATION)
                & (ReliableMessage.operation_id == command.command_code),
            )
            .where(
                command.owner_domain == owner_domain,
                command.command_code.in_(command_codes),
                ReliableMessage.id.in_(candidate_ids),
            )
        )
        return dict(rows.all())
