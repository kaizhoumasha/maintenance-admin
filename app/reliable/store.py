import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID

from sqlalchemy import and_, bindparam, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identifiers import uuid7
from app.diagnostics import log_exception
from app.http_client import DeliveryState, HttpRequest, HttpResult
from app.models.reliable import ReliableMessage
from app.transactions import after_commit

from .config import TargetConfig, positive
from .policy import retry_delay_s
from .types import (
    REPLACEMENT_PENDING,
    DeliverySnapshot,
    FailReason,
    MessageStatus,
    OutgoingMessage,
    ScopeRef,
    Verdict,
    VerdictKind,
)

logger = logging.getLogger(__name__)
TERMINAL = (
    MessageStatus.DONE,
    MessageStatus.FAILED,
    MessageStatus.EXPIRED,
    MessageStatus.ABANDONED,
)


@dataclass(frozen=True)
class Claim:
    message_id: int
    message: OutgoingMessage
    lease_id: str
    lease_until: datetime
    attempt_no: int
    not_received_count: int = 0
    delivery_uncertain: bool = False
    dispatch_started_at: datetime | None = None
    lease_not_started: bool | None = field(default=None, kw_only=True)
    dispatch_origin: str | None = field(default=None, kw_only=True)
    dispatch_protocol: str | None = field(default=None, kw_only=True)
    request: HttpRequest | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class AttemptOutcome:
    status: MessageStatus
    fail_reason: FailReason | None = None
    result: HttpResult | None = None
    verdict: Verdict | None = None
    next_attempt_at: datetime | None = None
    # None 表示保持原值;发送结果据此写入未送达计数与未知事实。
    not_received_count: int | None = None
    delivery_uncertain: bool | None = None
    pause_reason: str | None = None
    observation_record_id: UUID | None = field(default=None, kw_only=True)


def utc_datetime(value: datetime | None, *, known_sqlite_utc: bool = False) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        if not known_sqlite_utc:
            raise ValueError("unknown naive datetime source")
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class MessageStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        records,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uniform: Callable[[float, float], float] = random.uniform,
        lease_id_factory: Callable[[], str] = lambda: uuid7().hex,
        lease_margin_s: float = 5,
        lifecycles=None,
        served_snapshot_protocols=None,
        on_result: Callable[[str, str | None, str], None] | None = None,
        on_delivered: Callable[[AsyncSession, OutgoingMessage], Awaitable[bool | None]]
        | None = None,
    ):
        positive(lease_margin_s, "lease_margin_s")
        self.session_factory = session_factory
        self.records = records
        self.clock = clock
        self.uniform = uniform
        self.lease_id_factory = lease_id_factory
        self.lease_margin_s = lease_margin_s
        from .lifecycle import SnapshotTransactions

        self.snapshots = SnapshotTransactions(self, lifecycles or {})
        self.served_snapshot_protocols = served_snapshot_protocols
        self.on_result = on_result
        self.on_delivered = on_delivered
        self._skipped_claim_cursor = None

    def _time(self, session, value):
        return utc_datetime(value, known_sqlite_utc=session.get_bind().dialect.name == "sqlite")

    async def _claim(self, session, row, *, include_snapshot=True, bodies=None):
        if bodies is None:
            bodies = await self.records.frozen_requests(session, rows=(row,))
        message = OutgoingMessage(
            row.target,
            row.operation,
            row.operation_id,
            bodies[row.request_record_id],
            row.business_key,
            row.trace_id,
            self._time(session, row.created_at),
            self._time(session, row.expires_at),
            DeliverySnapshot(**row.delivery_snapshot)
            if row.delivery_snapshot is not None and include_snapshot
            else None,
            ScopeRef(row.scope_owner, row.scope_kind, row.scope_id)
            if row.scope_owner is not None
            else None,
            exchange_id=row.exchange_id,
            request_record_id=row.request_record_id,
        )
        return Claim(
            row.id,
            message,
            row.lease_id,
            self._time(session, row.lease_until),
            row.attempts + 1,
            row.not_received_count,
            bool(row.delivery_uncertain),
            self._time(session, row.dispatch_started_at),
            lease_not_started=row.lease_not_started,
        )

    async def claim(self, limit: int, targets: Mapping[str, TargetConfig]) -> tuple[Claim, ...]:
        if not targets:
            return ()
        now = utc_datetime(self.clock())
        async with self.session_factory() as session, session.begin():
            after_skip = True
            if self._skipped_claim_cursor is not None:
                skipped_at, skipped_id = self._skipped_claim_cursor
                after_skip = or_(
                    ReliableMessage.next_attempt_at > skipped_at,
                    and_(
                        ReliableMessage.next_attempt_at == skipped_at,
                        ReliableMessage.id > skipped_id,
                    ),
                )
            rows = await session.scalars(
                select(ReliableMessage)
                .where(
                    # 只领取本进程能服务的目标;其余消息保持 PENDING,不因配置漂移被终态失败。
                    ReliableMessage.target.in_(list(targets)),
                    after_skip,
                    ReliableMessage.status == MessageStatus.PENDING,
                    ReliableMessage.next_attempt_at <= now,
                    or_(
                        ReliableMessage.pause_reason.is_distinct_from(REPLACEMENT_PENDING),
                        ReliableMessage.delivery_uncertain,
                        ReliableMessage.attempts != ReliableMessage.not_received_count,
                    ),
                    (ReliableMessage.expires_at.is_(None)) | (ReliableMessage.expires_at > now),
                )
                .order_by(ReliableMessage.next_attempt_at, ReliableMessage.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = list(rows)
            bodies = await self.records.frozen_requests(session, rows=rows)
            if not rows:
                self._skipped_claim_cursor = None
            claims = []
            for row in rows:
                if row.delivery_snapshot is not None:
                    try:
                        snapshot = DeliverySnapshot(**row.delivery_snapshot)
                        targets[row.target].effective(snapshot)
                        if snapshot.protocol not in self.snapshots.lifecycles:
                            raise ValueError("snapshot lifecycle unavailable")
                        if (
                            self.served_snapshot_protocols is not None
                            and snapshot.protocol not in self.served_snapshot_protocols
                        ):
                            raise ValueError("snapshot sending assembly unavailable")
                    except (ValueError, TypeError, KeyError) as error:
                        self._skipped_claim_cursor = (
                            self._time(session, row.next_attempt_at),
                            row.id,
                        )
                        log_exception(
                            logger,
                            "snapshot claim unavailable",
                            error,
                            component="reliable",
                            phase="claim",
                            context={"message_id": row.id},
                        )
                        continue
                timeout = targets[row.target].timeout_ms / 1000
                row.status = MessageStatus.SENDING
                row.lease_id = self.lease_id_factory()
                row.lease_not_started = True
                row.lease_until = now + timedelta(seconds=timeout + self.lease_margin_s)
                row.last_attempt_at = now
                row.updated_at = now
                claims.append(await self._claim(session, row, bodies=bodies))
        for claim in claims:
            self._log(claim.message, MessageStatus.PENDING, MessageStatus.SENDING)
        return tuple(claims)

    async def before_send(self, claim, target):
        from .lifecycle import evidence_transaction

        if claim.message.delivery_snapshot is not None:
            return await self.snapshots.gate(claim, target)
        async with evidence_transaction(self.session_factory) as session:
            row = await session.scalar(
                select(ReliableMessage)
                .where(
                    ReliableMessage.id == claim.message_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            now = utc_datetime(self.clock())
            if row is None or row.status != "SENDING" or row.lease_id != claim.lease_id:
                return None
            if (
                row.pause_reason == REPLACEMENT_PENDING
                and not row.delivery_uncertain
                and row.attempts == row.not_received_count
            ):
                if row.lease_not_started is not True:
                    return None
                await self.records.close_attempt(
                    session,
                    message_id=row.id,
                    lease_id=row.lease_id,
                    at=now,
                    reason="SUPERSEDED",
                )
                row.status = MessageStatus.PENDING
                row.lease_id = row.lease_until = None
                row.next_attempt_at = row.updated_at = now
                return None
            if self._time(session, row.lease_until) <= now or (
                claim.message.expires_at is not None and claim.message.expires_at <= now
            ):
                return None
            row.lease_not_started = False
            if row.scope_owner is not None:
                row.dispatch_started_at = row.dispatch_started_at or now
            until = now + timedelta(seconds=target.timeout_ms / 1000 + self.lease_margin_s)
            row.lease_until = until
            await self.records.opened(
                session,
                claim=replace(
                    claim, dispatch_origin=target.base_url, dispatch_protocol=target.protocol
                ),
                at=now,
            )
            return until

    async def maintain_snapshots(self, limit, targets, deadline):
        return await self.snapshots.maintain(limit, targets, deadline)

    async def finish(self, claim: Claim, outcome: AttemptOutcome) -> bool:
        from .lifecycle import evidence_transaction

        if claim.message.delivery_snapshot is not None:
            return await self.snapshots.finish(claim, outcome)
        if outcome.status not in (
            MessageStatus.DONE,
            MessageStatus.FAILED,
            MessageStatus.EXPIRED,
            MessageStatus.PENDING,
            MessageStatus.STALLED,
        ):
            raise ValueError("invalid attempt transition")
        if outcome.status == MessageStatus.FAILED and outcome.fail_reason is None:
            raise ValueError("failed outcome requires reason")
        if outcome.status != MessageStatus.FAILED and outcome.fail_reason is not None:
            raise ValueError("unexpected failure reason")
        if outcome.status == MessageStatus.PENDING and outcome.next_attempt_at is None:
            raise ValueError("retry requires next_attempt_at")
        if outcome.status == MessageStatus.STALLED and outcome.next_attempt_at is not None:
            raise ValueError("stalled message has no schedule")
        now = utc_datetime(self.clock())
        result, verdict = outcome.result, outcome.verdict
        if outcome.status == MessageStatus.DONE and (
            verdict is None or verdict.kind != VerdictKind.DELIVERED
        ):
            raise ValueError("done requires delivered verdict")
        if verdict is not None and (
            result is None
            or result.delivery_state != DeliveryState.HTTP_RESPONSE_COMPLETE
            or not isinstance(result.body, bytes)
        ):
            raise ValueError("verdict requires complete HTTP response")
        if (
            verdict is not None
            and verdict.kind == VerdictKind.DELIVERED
            and verdict.response != result.body
        ):
            raise ValueError("response bytes must remain unchanged")
        if (
            outcome.status == MessageStatus.FAILED
            and outcome.fail_reason != FailReason.LOCAL_ERROR
            and (
                verdict is None
                or verdict.kind != VerdictKind.PEER_TERMINAL
                or verdict.reason != outcome.fail_reason
            )
        ):
            raise ValueError("peer failure requires matching verdict")
        if outcome.status == MessageStatus.EXPIRED and (
            result is not None
            or verdict is not None
            or claim.message.expires_at is None
            or claim.message.expires_at > now
        ):
            raise ValueError("expiry requires elapsed TTL and no send")
        keeps_final_response = verdict is not None and outcome.status in (
            MessageStatus.DONE,
            MessageStatus.FAILED,
        )
        values = dict(
            status=outcome.status,
            fail_reason=outcome.fail_reason,
            updated_at=now,
            lease_id=None,
            lease_until=None,
            attempts=ReliableMessage.attempts
            + (0 if outcome.status == MessageStatus.EXPIRED else 1),
            last_http_status=result.status_code if result else None,
            last_failure_kind=result.failure_kind if result else None,
            last_verdict=verdict.kind if verdict else None,
        )
        if outcome.next_attempt_at is not None:
            values["next_attempt_at"] = utc_datetime(outcome.next_attempt_at)
        if outcome.status == MessageStatus.STALLED:
            values["next_attempt_at"] = None
        if outcome.not_received_count is not None:
            values["not_received_count"] = outcome.not_received_count
        if outcome.delivery_uncertain is not None:
            values["delivery_uncertain"] = outcome.delivery_uncertain
        async with evidence_transaction(self.session_factory) as session:
            row = await session.scalar(
                select(ReliableMessage)
                .where(ReliableMessage.id == claim.message_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            active = row is not None and row.lease_id == claim.lease_id and row.status == "SENDING"
            observation = await self.records.finished(
                session, claim=claim, outcome=outcome, at=now, late=not active
            )
            if active:
                await self.records.close_attempt(
                    session,
                    message_id=claim.message_id,
                    lease_id=claim.lease_id,
                    at=now,
                    reason="FINISHED",
                    outcome=outcome,
                )
            values["last_response_record_id"] = None if keeps_final_response else observation
            values["last_response_wire"] = (
                result.body if result and not keeps_final_response else None
            )
            if keeps_final_response:
                values["terminal_response_record_id"] = observation
                values["terminal_response_wire"] = result.body if result else None
                values["terminal_response_at"] = now
            changed = await session.scalar(
                update(ReliableMessage)
                .where(
                    ReliableMessage.id == claim.message_id,
                    ReliableMessage.lease_id == claim.lease_id,
                    ReliableMessage.status == MessageStatus.SENDING,
                )
                .values(**values)
                .returning(ReliableMessage.id)
            )
            if changed is not None:
                handled = False
                if outcome.status == MessageStatus.DONE and self.on_delivered is not None:
                    handled = await self.on_delivered(session, claim.message)
                if not handled:
                    self._notify_terminal(session, claim.message, outcome.status)
        if changed is not None:
            self._log(
                claim.message,
                MessageStatus.SENDING,
                outcome.status,
                reason=outcome.fail_reason,
                attempts=claim.attempt_no,
            )
        return changed is not None

    def _notify_terminal(self, session, message, status) -> None:
        if status in TERMINAL and self.on_result is not None:
            after_commit(
                session,
                partial(
                    self.on_result, message.operation, message.business_key, message.operation_id
                ),
            )

    async def recover_expired(self, limit: int, targets: Mapping[str, TargetConfig]) -> int:
        now = utc_datetime(self.clock())
        async with self.session_factory() as session, session.begin():
            rows = list(
                await session.scalars(
                    select(ReliableMessage)
                    .where(
                        ReliableMessage.status == MessageStatus.SENDING,
                        ReliableMessage.delivery_snapshot.is_(None),
                        ReliableMessage.lease_until <= now,
                    )
                    .order_by(ReliableMessage.lease_until, ReliableMessage.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            bodies = await self.records.frozen_requests(session, rows=rows)
            claims = [await self._claim(session, row, bodies=bodies) for row in rows]
            if claims:
                # 行已被 FOR UPDATE 锁定,无需再带租约条件;一次批量执行代替逐行往返。
                table = ReliableMessage.__table__
                recoveries = [
                    (claim, self._recovery_values(claim, now, targets)) for claim in claims
                ]
                for claim in claims:
                    await self.records.close_attempt(
                        session,
                        message_id=claim.message_id,
                        lease_id=claim.lease_id,
                        at=now,
                        reason="LEASE_EXPIRED",
                    )
                await session.execute(
                    update(table).where(table.c.id == bindparam("row_id")),
                    [
                        {
                            "row_id": claim.message_id,
                            "attempts": claim.attempt_no,
                            **values,
                        }
                        for claim, values in recoveries
                    ],
                )
                for claim, values in recoveries:
                    self._notify_terminal(session, claim.message, values["status"])
        for claim in claims:
            self._recovery_log(claim, now, targets)
        return len(rows)

    async def release(self, claim: Claim, targets: Mapping[str, TargetConfig]) -> bool:
        if claim.message.delivery_snapshot is not None:
            return await self.snapshots.release(claim)
        now = utc_datetime(self.clock())
        async with self.session_factory() as session, session.begin():
            changed = await self._recover(session, claim, now, targets)
        if changed:
            self._recovery_log(claim, now, targets)
        return changed

    def _recovery_log(self, claim, now, targets):
        status = (
            MessageStatus.EXPIRED
            if claim.message.expires_at is not None and claim.message.expires_at <= now
            else MessageStatus.PENDING
        )
        self._log(
            claim.message,
            MessageStatus.SENDING,
            status,
            attempts=claim.attempt_no,
            recovery="unknown",
        )
        target = targets.get(claim.message.target)
        if (
            status == MessageStatus.PENDING
            and target
            and claim.attempt_no >= target.alert_after_attempts
        ):
            logger.warning(
                "reliable retry threshold target=%r operation=%r operation_id=%r attempts=%s",
                claim.message.target,
                claim.message.operation,
                claim.message.operation_id,
                claim.attempt_no,
            )

    def _recovery_values(self, claim, now, targets):
        target = targets.get(claim.message.target)
        if target is None:
            target = TargetConfig("missing", "http://invalid.test", "missing")
        expired = claim.message.expires_at is not None and claim.message.expires_at <= now
        return dict(
            status=MessageStatus.EXPIRED if expired else MessageStatus.PENDING,
            updated_at=now,
            lease_id=None,
            lease_until=None,
            last_http_status=None,
            last_failure_kind=None,
            last_verdict=None,
            last_response_record_id=None,
            last_response_wire=None,
            # 租约中断后请求可能已送达:一旦未知,预算不再适用(合同 §4.3)。
            delivery_uncertain=claim.delivery_uncertain
            or claim.lease_not_started is not True
            or claim.dispatch_started_at is not None
            or claim.message.scope is None,
            next_attempt_at=now
            + timedelta(seconds=retry_delay_s(claim.attempt_no, target, None, self.uniform)),
        )

    async def _recover(self, session, claim, now, targets):
        active = (
            ReliableMessage.id == claim.message_id,
            ReliableMessage.lease_id == claim.lease_id,
            ReliableMessage.status == MessageStatus.SENDING,
        )
        identity = await session.scalar(select(ReliableMessage.id).where(*active).with_for_update())
        if identity is None:
            return False
        await self.records.close_attempt(
            session,
            message_id=identity,
            lease_id=claim.lease_id,
            at=now,
            reason="LEASE_RELEASED",
        )
        values = self._recovery_values(claim, now, targets)
        values["delivery_uncertain"] = or_(
            ReliableMessage.delivery_uncertain,
            ReliableMessage.lease_not_started.is_not(True),
            ReliableMessage.dispatch_started_at.is_not(None),
            ReliableMessage.scope_owner.is_(None),
        )
        changed = await session.scalar(
            update(ReliableMessage)
            .where(*active)
            .values(
                attempts=ReliableMessage.attempts + 1,
                **values,
            )
            .returning(ReliableMessage.id)
        )
        if changed is not None:
            self._notify_terminal(session, claim.message, values["status"])
        return changed is not None

    async def expire_pending(self, limit: int) -> int:
        now = utc_datetime(self.clock())
        async with self.session_factory() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(ReliableMessage.id)
                    .where(
                        ReliableMessage.status == MessageStatus.PENDING,
                        ReliableMessage.delivery_snapshot.is_(None),
                        ReliableMessage.expires_at <= now,
                    )
                    .order_by(ReliableMessage.expires_at, ReliableMessage.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if not ids:
                return 0
            changed = list(
                (
                    await session.execute(
                        update(ReliableMessage)
                        .where(
                            ReliableMessage.id.in_(ids),
                            ReliableMessage.status == MessageStatus.PENDING,
                            ReliableMessage.expires_at <= now,
                        )
                        .values(status=MessageStatus.EXPIRED, updated_at=now)
                        .returning(
                            ReliableMessage.target,
                            ReliableMessage.operation,
                            ReliableMessage.business_key,
                            ReliableMessage.operation_id,
                            ReliableMessage.trace_id,
                            ReliableMessage.attempts,
                        )
                    )
                ).all()
            )
            for message in changed:
                self._notify_terminal(session, message, MessageStatus.EXPIRED)
        for message in changed:
            self._log(
                message, MessageStatus.PENDING, MessageStatus.EXPIRED, attempts=message.attempts
            )
        return len(changed)

    @staticmethod
    def _log(message, previous, status, **fields):
        level = (
            logging.ERROR
            if fields.get("reason") == FailReason.LOCAL_ERROR
            else (
                logging.WARNING
                if status in (MessageStatus.FAILED, MessageStatus.STALLED)
                else logging.INFO
            )
        )
        context = {
            "target": message.target,
            "operation": message.operation,
            "operation_id": message.operation_id,
            "message_trace_id": message.trace_id,
            "from_status": previous,
            "to_status": status,
            **fields,
        }
        logger.log(level, "reliable transition %r", context, extra=context)


CLEANUP_BATCH_SIZE = 1000


async def cleanup_terminal(
    session: AsyncSession,
    *,
    now: datetime,
    retention_days: int,
    limit: int | None = None,
    protected_ids: frozenset[int] = frozenset(),
    candidate_ids: tuple[int, ...] | None = None,
) -> int:
    positive(retention_days, "retention_days")
    cutoff = utc_datetime(now) - timedelta(days=retention_days)
    expired = select(ReliableMessage.id).where(
        ReliableMessage.status.in_(TERMINAL),
        ReliableMessage.updated_at < cutoff,
    )
    if candidate_ids is not None:
        expired = expired.where(ReliableMessage.id.in_(candidate_ids))
    expired = expired.where(ReliableMessage.id.not_in(protected_ids))
    if limit is not None:
        expired = expired.order_by(ReliableMessage.id).limit(limit)
    result = await session.execute(delete(ReliableMessage).where(ReliableMessage.id.in_(expired)))
    return result.rowcount
