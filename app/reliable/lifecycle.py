"""Transaction ports for snapshot delivery; implementations may only use this session."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.diagnostics import log_exception
from app.models.reliable import ReliableMessage

from .types import RETRY_ACK_TIMEOUT, MessageStatus, VerdictKind

logger = logging.getLogger(__name__)


class EvidencePersistenceError(Exception):
    pass


class EvidenceIntegrityError(EvidencePersistenceError):
    pass


class PreDispatchTimeout(EvidencePersistenceError):
    pass


def database_failure_code(error):
    original = error.__cause__ if isinstance(error, EvidencePersistenceError) else error
    return getattr(original.orig, "sqlstate", None) if isinstance(original, DBAPIError) else None


class DeliveryRecords(Protocol):
    async def register(self, session, *, message, protocol, exchange_id=None): ...
    async def requests(self, session, *, record_ids): ...
    async def frozen_requests(self, session, *, rows): ...
    async def link_message(self, session, *, exchange_id, message_id, at): ...
    async def opened(self, session, *, claim, at): ...
    async def finished(self, session, *, claim, outcome, at, late): ...
    async def close_attempt(self, session, *, message_id, lease_id, at, reason, outcome=None): ...


@asynccontextmanager
async def evidence_transaction(factory):
    try:
        async with factory() as session, session.begin():
            yield session
    except SQLAlchemyError as error:
        raise EvidencePersistenceError("integration transaction unavailable") from error


class DeliveryLifecycle(Protocol):
    async def lock_context(self, session, identity, *, nowait=False): ...
    async def authorize(self, session, context): ...
    async def check_dispatch(self, factory, claim, clock): ...
    def may_reclaim_unstarted(self, context): ...
    async def before_send(self, session, context, claim, now): ...
    async def apply_transition(self, session, context, claim, outcome): ...
    async def observe_late(self, session, context, claim, outcome): ...


async def ensure_savepoint_transaction(session: AsyncSession) -> None:
    """SQLite legacy mode must start a real transaction before the first savepoint."""
    if session.get_bind().dialect.name == "sqlite":
        connection = await session.connection()
        raw_connection = await connection.get_raw_connection()
        if not raw_connection.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")


async def transaction_limits(session, seconds=5):
    if seconds <= 0:
        raise TimeoutError("transaction deadline elapsed")
    if session.get_bind().dialect.name == "postgresql":
        milliseconds = max(1, int(seconds * 1000))
        await session.execute(
            text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{milliseconds}ms"}
        )
        await session.execute(
            text("SELECT set_config('statement_timeout', :v, true)"), {"v": f"{milliseconds}ms"}
        )


class SnapshotTransactions:
    def __init__(self, store, lifecycles):
        self.store, self.lifecycles = store, lifecycles
        self.cursor = 0

    def port(self, message):
        snapshot = message.delivery_snapshot
        if snapshot is None:
            raise ValueError("snapshot required")
        return self.lifecycles[snapshot.protocol]

    async def locked(self, session, claim, *, nowait=False, authorize=False):
        await transaction_limits(session)
        port = self.port(claim.message)
        context = await port.lock_context(session, claim.message.operation_id, nowait=nowait)
        authorization = await port.authorize(session, context) if authorize else None
        row = await session.scalar(
            select(ReliableMessage)
            .where(ReliableMessage.id == claim.message_id)
            .with_for_update(nowait=nowait)
            .execution_options(populate_existing=True)
        )
        return port, context, row, authorization

    async def check_dispatch(self, claim):
        outcome = await self.port(claim.message).check_dispatch(
            self.store.session_factory, claim, self.store.clock
        )
        if outcome is None:
            return True
        if outcome.status == MessageStatus.PENDING:
            async with evidence_transaction(self.store.session_factory) as session:
                row = await session.get(ReliableMessage, claim.message_id, with_for_update=True)
                if self.unstarted(row, claim):
                    self.defer(row, outcome.next_attempt_at, outcome.pause_reason)
        else:
            await self.finish(claim, outcome)
        return False

    @staticmethod
    def unstarted(row, claim):
        return (
            row is not None
            and row.status == "SENDING"
            and row.lease_id == claim.lease_id
            and row.lease_not_started is True
        )

    def defer(self, row, next_attempt_at, reason=None):
        row.status = MessageStatus.PENDING
        row.pause_reason = reason
        row.next_attempt_at = next_attempt_at
        row.updated_at = self.store.clock()
        row.lease_id = row.lease_until = None

    async def reclaim_unstarted(self, session, port, context, row, claim):
        if not (
            self.unstarted(row, claim)
            and not row.delivery_uncertain
            and port.may_reclaim_unstarted(context)
        ):
            return False
        now = self.store.clock()
        if claim.message.expires_at is not None and claim.message.expires_at <= now:
            from .store import AttemptOutcome

            await self.apply(
                session, port, context, row, claim, AttemptOutcome(MessageStatus.EXPIRED)
            )
        else:
            self.defer(row, now)
        return True

    async def gate(self, claim, target):
        async with evidence_transaction(self.store.session_factory) as session:
            try:
                port, context, row, authorization = await self.locked(
                    session, claim, authorize=True
                )
            except DBAPIError as error:
                code = database_failure_code(error)
                # Equal PostgreSQL timeout budgets can report the lock wait as a statement timeout.
                if code != "55P03" and not (
                    code == "57014"
                    and str(error.orig) == "canceling statement due to statement timeout"
                ):
                    raise
                await session.rollback()
                raise PreDispatchTimeout("send gate timed out and rolled back") from error
            now = self.store.clock()
            if row is None or row.status != "SENDING" or row.lease_id != claim.lease_id:
                return None
            if self.store._time(session, row.lease_until) <= now:
                return None
            from .store import AttemptOutcome

            if claim.message.expires_at is not None and claim.message.expires_at <= now:
                outcome = AttemptOutcome(MessageStatus.EXPIRED)
            else:
                try:
                    target.effective(claim.message.delivery_snapshot)
                    reason = authorization or await port.before_send(session, context, claim, now)
                except ValueError:
                    reason = "CONFIG_DRIFT"
                if not reason:
                    now = self.store.clock()
                    if self.store._time(session, row.lease_until) <= now or (
                        claim.message.expires_at is not None and claim.message.expires_at <= now
                    ):
                        await session.rollback()
                        return None
                    until = now + timedelta(
                        seconds=target.timeout_ms / 1000 + self.store.lease_margin_s
                    )
                    if row.scope_owner is not None:
                        row.dispatch_started_at = row.dispatch_started_at or now
                    row.lease_until = until
                    row.lease_not_started = False
                    await self.store.records.opened(
                        session,
                        claim=replace(
                            claim,
                            dispatch_protocol=target.protocol,
                            dispatch_origin=target.effective(
                                claim.message.delivery_snapshot
                            ).base_url,
                        ),
                        at=now,
                    )
                    return until
                outcome = AttemptOutcome(
                    MessageStatus.HELD, pause_reason=reason, delivery_uncertain=None
                )
            await self.apply(session, port, context, row, claim, outcome)
            return None

    async def finish(self, claim, outcome, *, close_reason="FINISHED"):
        allowed = {
            MessageStatus.PENDING,
            MessageStatus.HELD,
            MessageStatus.DONE,
            MessageStatus.FAILED,
            MessageStatus.EXPIRED,
        }
        if outcome.status not in allowed:
            raise ValueError("invalid snapshot transition")
        if outcome.status == MessageStatus.PENDING and outcome.next_attempt_at is None:
            raise ValueError("retry requires schedule")
        if outcome.status == MessageStatus.HELD and (
            not outcome.pause_reason or outcome.next_attempt_at is not None
        ):
            raise ValueError("HELD requires reason and no schedule")
        if outcome.status == MessageStatus.DONE and (
            outcome.verdict is None
            or outcome.verdict.kind != VerdictKind.DELIVERED
            or outcome.result is None
            or outcome.verdict.response != outcome.result.body
        ):
            raise ValueError("DONE requires matching ACK evidence")
        if outcome.status == MessageStatus.FAILED and (
            outcome.verdict is None
            or outcome.verdict.kind != VerdictKind.PEER_TERMINAL
            or outcome.fail_reason != outcome.verdict.reason
        ):
            raise ValueError("FAILED requires matching rejection evidence")
        async with evidence_transaction(self.store.session_factory) as session:
            port, context, row, _ = await self.locked(session, claim)
            if outcome.pause_reason == "LEASE_INTERRUPTED" and await self.reclaim_unstarted(
                session, port, context, row, claim
            ):
                return True
            outcome = self._interruption(row, outcome)
            if row is None or row.status != "SENDING" or row.lease_id != claim.lease_id:
                observation = await self.store.records.finished(
                    session, claim=claim, outcome=outcome, at=self.store.clock(), late=True
                )
                outcome = replace(outcome, observation_record_id=observation)
                await port.observe_late(session, context, claim, outcome)
                return False
            await self.apply(session, port, context, row, claim, outcome, close_reason=close_reason)
            return True

    @staticmethod
    def _interruption(row, outcome):
        if (
            row is not None
            and outcome.pause_reason == "LEASE_INTERRUPTED"
            and row.scope_owner is not None
            and row.dispatch_started_at is None
            and not row.delivery_uncertain
        ):
            return replace(outcome, delivery_uncertain=False)
        return outcome

    async def apply(self, session, port, context, row, claim, outcome, *, close_reason="FINISHED"):
        outcome = self._interruption(row, outcome)
        snapshot = claim.message.delivery_snapshot
        if (
            snapshot
            and snapshot.unknown_policy == RETRY_ACK_TIMEOUT
            and row.delivery_uncertain
            and (
                outcome.status in (MessageStatus.EXPIRED, MessageStatus.FAILED)
                or outcome.pause_reason == "CONTRACT_NON_COMPLIANT"
            )
        ):
            # Stop sending without treating a prior uncertain attempt as rejected.
            outcome = replace(
                outcome,
                status=MessageStatus.HELD,
                fail_reason=None,
                pause_reason="DELIVERY_UNKNOWN",
            )
        now = self.store.clock()
        observation = await self.store.records.finished(
            session, claim=claim, outcome=outcome, at=now, late=False
        )
        outcome = replace(outcome, observation_record_id=observation)
        await self.store.records.close_attempt(
            session,
            message_id=row.id,
            lease_id=row.lease_id,
            at=now,
            reason=close_reason,
            outcome=outcome,
        )
        row.status = outcome.status
        row.pause_reason = outcome.pause_reason
        row.fail_reason = outcome.fail_reason
        row.lease_id = row.lease_until = None
        row.next_attempt_at = (
            None if outcome.status == MessageStatus.HELD else (outcome.next_attempt_at or now)
        )
        row.updated_at = now
        row.attempts += 1
        if outcome.delivery_uncertain is not None:
            row.delivery_uncertain = outcome.delivery_uncertain
        if outcome.not_received_count is not None:
            row.not_received_count = outcome.not_received_count
        if outcome.result:
            row.last_response_record_id = observation
            row.last_response_wire = outcome.result.body
            row.last_http_status = outcome.result.status_code
            row.last_failure_kind = outcome.result.failure_kind
        if outcome.verdict:
            row.last_verdict = outcome.verdict.kind
            if (
                outcome.verdict.kind == VerdictKind.DELIVERED
                or outcome.status == MessageStatus.FAILED
            ):
                row.terminal_response_record_id = observation
                row.terminal_response_wire = outcome.result.body if outcome.result else None
                row.terminal_response_at = now
        await port.apply_transition(session, context, claim, outcome)

    async def release(self, claim):
        from .store import AttemptOutcome

        return await self.finish(
            claim,
            AttemptOutcome(
                MessageStatus.HELD, pause_reason="LEASE_INTERRUPTED", delivery_uncertain=True
            ),
            close_reason="LEASE_RELEASED",
        )

    async def maintain(self, limit, targets, deadline):
        from .store import AttemptOutcome

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        count = 0
        try:
            async with asyncio.timeout(remaining):
                async with self.store.session_factory() as session:
                    await transaction_limits(session, deadline - time.monotonic())
                    ids = list(
                        await session.scalars(
                            select(ReliableMessage.id)
                            .where(
                                ReliableMessage.delivery_snapshot.is_not(None),
                                ReliableMessage.status.in_(("PENDING", "SENDING")),
                                ReliableMessage.id > self.cursor,
                            )
                            .order_by(ReliableMessage.id)
                            .limit(limit)
                        )
                    )
                if not ids:
                    self.cursor = 0
                for identity in ids:
                    self.cursor = identity
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.01:
                        break
                    try:
                        async with self.store.session_factory() as session, session.begin():
                            await transaction_limits(session, remaining)
                            row = await session.get(ReliableMessage, identity)
                            snapshot = row.delivery_snapshot or {}
                            port = self.lifecycles[snapshot.get("protocol")]
                            context = await port.lock_context(
                                session, row.operation_id, nowait=True
                            )
                            row = await session.scalar(
                                select(ReliableMessage)
                                .where(ReliableMessage.id == identity)
                                .with_for_update(nowait=True)
                                .execution_options(populate_existing=True)
                            )
                            now = self.store.clock()
                            if row is None or row.status not in ("PENDING", "SENDING"):
                                continue
                            reason = None
                            try:
                                claim = await self.store._claim(session, row)
                            except (TypeError, ValueError):
                                claim = await self.store._claim(
                                    session, row, include_snapshot=False
                                )
                                reason = "CONFIG_DRIFT"
                            if not reason and row.status == "SENDING":
                                if self.store._time(session, row.lease_until) > now:
                                    continue
                                if await self.reclaim_unstarted(session, port, context, row, claim):
                                    count += 1
                                    continue
                                reason = "LEASE_INTERRUPTED"
                            elif not reason:
                                target = targets.get(row.target)
                                try:
                                    if target is None:
                                        raise ValueError("target missing")
                                    target.effective(claim.message.delivery_snapshot)
                                    if (
                                        self.store.served_snapshot_protocols is not None
                                        and target.protocol
                                        not in self.store.served_snapshot_protocols
                                    ):
                                        reason = "ASSEMBLY_UNAVAILABLE"
                                except (ValueError, TypeError):
                                    reason = "CONFIG_DRIFT"
                            if reason:
                                outcome = AttemptOutcome(
                                    MessageStatus.HELD,
                                    pause_reason=reason,
                                    delivery_uncertain=True if row.status == "SENDING" else None,
                                )
                            elif (
                                row.expires_at is not None
                                and self.store._time(session, row.expires_at) <= now
                            ):
                                outcome = AttemptOutcome(MessageStatus.EXPIRED)
                            else:
                                continue
                            await self.apply(
                                session,
                                port,
                                context,
                                row,
                                claim,
                                outcome,
                                close_reason="LEASE_EXPIRED"
                                if reason == "LEASE_INTERRUPTED"
                                else "FINISHED",
                            )
                            count += 1
                    except Exception as error:
                        log_exception(
                            logger,
                            "snapshot maintenance unavailable",
                            error,
                            component="reliable",
                            phase="maintain_snapshots",
                            context={"message_id": identity},
                        )
        except TimeoutError:
            logger.warning("snapshot maintenance deadline reached")
        return count
