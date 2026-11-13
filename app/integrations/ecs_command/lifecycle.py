from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select

from app.http_client import DeliveryState
from app.integrations.history import RecordDraft, read_bodies, write_record
from app.integrations.runtime_authority import enabled
from app.models import EcsCommand, EcsSendEvidence, IntegrationExchange, IntegrationRecord
from app.reliable.types import VerdictKind
from app.transactions import after_commit

from .contracts import PauseReason
from .routing import RouteAuthorization


async def get_attempt_conclusions(
    session, *, exchange_id: UUID, require_open: bool = False
) -> dict[str, str]:
    if enabled():
        with session.no_autoflush:
            rows = await session.execute(
                select(
                    EcsSendEvidence.lease_id,
                    EcsSendEvidence.attempt_revision,
                    EcsSendEvidence.outcome,
                )
                .join(EcsCommand, EcsCommand.command_code == EcsSendEvidence.command_code)
                .where(EcsCommand.id == exchange_id)
            )
        rows = [
            (
                lease,
                "SEND_ATTEMPT_OPENED" if revision == 0 else "ECS_ATTEMPT_CONCLUSION",
                revision,
                outcome,
            )
            for lease, revision, outcome in rows
        ]
    else:
        with session.no_autoflush:
            rows = await session.execute(
                select(
                    IntegrationRecord.attempt_id,
                    IntegrationRecord.record_kind,
                    IntegrationRecord.attempt_revision,
                    IntegrationRecord.ecs_attempt_outcome,
                ).where(
                    IntegrationRecord.exchange_id == exchange_id,
                    IntegrationRecord.record_kind.in_(
                        ("SEND_ATTEMPT_OPENED", "ECS_ATTEMPT_CONCLUSION")
                    ),
                )
            )
    opened, conclusions = set(), {}
    for attempt, kind, revision, outcome in rows:
        if kind == "SEND_ATTEMPT_OPENED":
            opened.add(attempt)
        elif attempt not in conclusions or conclusions[attempt][0] < revision:
            conclusions[attempt] = (revision, outcome)
    return {
        attempt: conclusions[attempt][1]
        if attempt in conclusions and (not require_open or attempt in opened)
        else "OPEN_ATTEMPT"
        for attempt in opened | conclusions.keys()
    }


class EcsLifecycle:
    def __init__(self, service, *, on_ack=None):
        self.service = service
        self.on_ack = on_ack

    async def lock_context(self, session, identity, *, nowait=False):
        model = EcsCommand if enabled() else IntegrationExchange
        command = await session.scalar(
            select(model)
            .where(model.command_code == identity)
            .with_for_update(nowait=nowait)
            .execution_options(populate_existing=True)
        )
        if command is None:
            raise ValueError("ECS command missing")
        return command

    async def authorize(self, session, command):
        authorization = await self.service.routes.authorize_frozen(session, command.route)
        if authorization != RouteAuthorization.ALLOW:
            return (
                "AUTHORIZATION_REVOKED"
                if authorization == RouteAuthorization.REVOKED
                else "PRE_GATE_VALIDATION"
            )
        return None

    async def before_send(self, session, command, claim, now):
        if (
            command.blocked
            or command.applied_result_id
            or command.status not in ("QUEUED", "DISPATCHING")
        ):
            return command.hold_reason or "PRE_GATE_VALIDATION"
        if isinstance(command, EcsCommand):
            wire = command.request_wire
            request_matches = command.id == claim.message.exchange_id
        else:
            bodies = await read_bodies(session, record_ids=(command.request_record_id,))
            stored = bodies.get(command.request_record_id)
            wire = stored.raw_body if stored is not None else None
            request_matches = command.request_record_id == claim.message.request_record_id
        if (
            wire != claim.message.request_raw
            or command.request_sha256 != sha256(claim.message.request_raw).hexdigest()
            or not request_matches
            or command.delivery_snapshot != asdict(claim.message.delivery_snapshot)
        ):
            return "CONFIG_DRIFT"
        command.dispatch_started_at = now
        command.status = "DISPATCHING"
        command.delivery_status = "SENDING"
        return None

    async def record(self, session, command, claim, outcome, *, late=False):
        if isinstance(command, EcsCommand):
            evidence = await session.get(EcsSendEvidence, (command.command_code, claim.lease_id, 0))
        else:
            evidence = await session.scalar(
                select(IntegrationRecord).where(
                    IntegrationRecord.exchange_id == command.id,
                    IntegrationRecord.record_kind == "SEND_ATTEMPT_OPENED",
                    IntegrationRecord.attempt_id == claim.lease_id,
                )
            )
        if evidence is None:
            # A pre-gate failure has no possible-dispatch evidence.
            return
        result, verdict = outcome.result, outcome.verdict
        definite = bool(result and result.delivery_state == DeliveryState.NOT_SENT) or bool(
            verdict and verdict.kind == VerdictKind.NOT_RECEIVED
        )
        if verdict and verdict.kind == VerdictKind.DELIVERED:
            kind = "ACK"
        elif verdict and verdict.kind == VerdictKind.PEER_TERMINAL:
            kind = "REJECTED"
        elif definite or (
            verdict
            and verdict.kind == VerdictKind.HOLD
            and verdict.pause_reason == "CONTRACT_NON_COMPLIANT"
        ):
            kind = "NOT_RECEIVED"
        else:
            kind = "UNKNOWN"
        if isinstance(command, EcsCommand):
            last = await session.scalar(
                select(EcsSendEvidence)
                .where(
                    EcsSendEvidence.command_code == command.command_code,
                    EcsSendEvidence.lease_id == claim.lease_id,
                )
                .order_by(EcsSendEvidence.attempt_revision.desc())
                .limit(1)
            )
            response = result.body if result else None
            if (
                last.attempt_revision == 0
                or last.outcome != kind
                or last.observation_id != outcome.observation_record_id
                or last.late != late
            ):
                session.add(
                    EcsSendEvidence(
                        command_code=command.command_code,
                        lease_id=claim.lease_id,
                        attempt_revision=last.attempt_revision + 1,
                        outcome=kind,
                        observation_id=outcome.observation_record_id,
                        occurred_at=datetime.now(UTC),
                        http_status=result.status_code if result else None,
                        response_wire=response,
                        late=late,
                    )
                )
            if kind == "ACK":
                command.ack_record_id = outcome.observation_record_id
                command.ack_wire = response
            if late and command.applied_result_id and kind in ("REJECTED", "NOT_RECEIVED"):
                command.needs_reconciliation = True
            return
        last = await session.scalar(
            select(IntegrationRecord)
            .where(
                IntegrationRecord.exchange_id == command.id,
                IntegrationRecord.record_kind == "ECS_ATTEMPT_CONCLUSION",
                IntegrationRecord.attempt_id == claim.lease_id,
            )
            .order_by(IntegrationRecord.attempt_revision.desc())
            .limit(1)
        )
        if last is None or (
            last.ecs_attempt_outcome != kind
            or last.parent_record_id != outcome.observation_record_id
            or last.late != late
        ):
            revision = 1 if last is None else last.attempt_revision + 1
            await write_record(
                session,
                draft=RecordDraft(
                    exchange_id=command.id,
                    record_kind="ECS_ATTEMPT_CONCLUSION",
                    event_key=f"ECS:{claim.lease_id}:{revision}",
                    attempt_id=claim.lease_id,
                    attempt_revision=revision,
                    ecs_attempt_outcome=kind,
                    direction="INTERNAL",
                    parent_record_id=outcome.observation_record_id,
                    occurred_at=datetime.now(UTC),
                    http_status=result.status_code if result else None,
                    late=late,
                ),
            )
        if kind == "ACK":
            command.ack_record_id = outcome.observation_record_id
        if late and command.applied_result_id and kind in ("REJECTED", "NOT_RECEIVED"):
            command.needs_reconciliation = True

    async def observe_late(self, session, command, claim, outcome):
        if outcome.result is None:
            return
        await self.record(session, command, claim, outcome, late=True)

    async def apply_transition(self, session, command, claim, outcome):
        if outcome.status == "HELD":
            PauseReason(outcome.pause_reason)
        if outcome.status == "DONE" and outcome.verdict is None and not command.applied_result_id:
            raise ValueError("ECS settlement requires an applicable durable result")
        if outcome.result is not None or outcome.delivery_uncertain is True:
            await self.record(session, command, claim, outcome)
        command.delivery_status = outcome.status
        if outcome.status == "ABANDONED":
            return
        if command.applied_result_id or command.blocked:
            return
        if outcome.status == "HELD":
            command.status = "RECONCILING"
            command.hold_reason = outcome.pause_reason
            if outcome.pause_reason == "IDENTITY_CONFLICT":
                command.blocked = True
                command.needs_reconciliation = True
        else:
            command.status = {
                "DONE": "WAITING_RESULT",
                "FAILED": "REJECTED",
                "EXPIRED": "EXPIRED",
            }.get(outcome.status, "QUEUED")
        if (
            self.on_ack is not None
            and command.status == "WAITING_RESULT"
            and command.ack_record_id is not None
            and outcome.verdict is not None
            and outcome.verdict.kind == VerdictKind.DELIVERED
        ):
            after_commit(session, partial(self.on_ack, command.owner_domain, command.task_type))
