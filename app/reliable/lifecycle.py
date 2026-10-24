"""Transaction ports for snapshot delivery; implementations may only use this session."""

import asyncio
import logging
import time
from datetime import timedelta
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.diagnostics import log_exception
from app.models.reliable import ReliableMessage

from .types import MessageStatus, VerdictKind

logger = logging.getLogger(__name__)


class DeliveryLifecycle(Protocol):
    async def lock_context(self, session, identity, *, nowait=False): ...
    async def authorize(self, session, context): ...
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

    async def gate(self, claim, target):
        async with self.store.session_factory() as session, session.begin():
            port, context, row, authorization = await self.locked(session, claim, authorize=True)
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
                    row.lease_until = until
                    return until
                outcome = AttemptOutcome(
                    MessageStatus.HELD, pause_reason=reason, delivery_uncertain=None
                )
            await self.apply(session, port, context, row, claim, outcome)
            return None

    async def finish(self, claim, outcome):
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
        async with self.store.session_factory() as session, session.begin():
            port, context, row, _ = await self.locked(session, claim)
            if row is None or row.status != "SENDING" or row.lease_id != claim.lease_id:
                await port.observe_late(session, context, claim, outcome)
                return False
            await self.apply(session, port, context, row, claim, outcome)
            return True

    async def apply(self, session, port, context, row, claim, outcome):
        now = self.store.clock()
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
            row.last_http_status = outcome.result.status_code
            row.last_failure_kind = outcome.result.failure_kind
            row.last_response_raw = outcome.result.body
        if outcome.verdict:
            row.last_verdict = outcome.verdict.kind
            if (
                outcome.verdict.kind == VerdictKind.DELIVERED
                or outcome.status == MessageStatus.FAILED
            ):
                row.response_raw = outcome.result.body
        await port.apply_transition(session, context, claim, outcome)

    async def release(self, claim):
        from .store import AttemptOutcome

        return await self.finish(
            claim,
            AttemptOutcome(
                MessageStatus.HELD, pause_reason="LEASE_INTERRUPTED", delivery_uncertain=True
            ),
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
                                claim = self.store._claim(session, row)
                            except (TypeError, ValueError):
                                claim = self.store._claim(session, row, include_snapshot=False)
                                reason = "CONFIG_DRIFT"
                            if not reason and row.status == "SENDING":
                                if self.store._time(session, row.lease_until) > now:
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
                            await self.apply(session, port, context, row, claim, outcome)
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
