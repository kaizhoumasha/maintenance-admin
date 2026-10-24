from dataclasses import asdict

from sqlalchemy import select

from app.http_client import DeliveryState
from app.models import EcsCommand, EcsSendEvidence
from app.reliable.types import VerdictKind

from .contracts import PauseReason
from .routing import RouteAuthorization


class EcsLifecycle:
    def __init__(self, service):
        self.service = service

    async def lock_context(self, session, identity, *, nowait=False):
        command = await session.scalar(
            select(EcsCommand)
            .where(EcsCommand.command_code == identity)
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
        if command.request_raw != claim.message.request_raw or command.delivery_snapshot != asdict(
            claim.message.delivery_snapshot
        ):
            return "CONFIG_DRIFT"
        session.add(
            EcsSendEvidence(
                command_code=command.command_code,
                lease_id=claim.lease_id,
                outcome="OPEN_ATTEMPT",
                created_at=now,
                late=False,
            )
        )
        command.dispatch_started_at = now
        command.status = "DISPATCHING"
        command.delivery_status = "SENDING"
        return None

    async def record(self, session, command, claim, outcome, *, late=False):
        evidence = await session.scalar(
            select(EcsSendEvidence).where(
                EcsSendEvidence.command_code == command.command_code,
                EcsSendEvidence.lease_id == claim.lease_id,
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
        evidence.outcome = kind
        evidence.response_raw = result.body if result else None
        evidence.http_status = result.status_code if result else None
        evidence.late = late
        if kind == "ACK":
            command.ack_raw = result.body
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
