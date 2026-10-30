import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_trace_id
from app.http_client import validate_request
from app.models.reliable import ReliableMessage

from .config import TargetConfig
from .store import utc_datetime
from .types import (
    ComparableRequests,
    DuplicateMessage,
    EnqueueResult,
    InvalidMessage,
    MessageDraft,
    MessageStatus,
    OutgoingMessage,
    ProtocolAdapter,
)

logger = logging.getLogger(__name__)


def _log_resumed(sync_session):
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    contexts = sync_session.info["reliable_resumed"].pop(transaction, [])
    if transaction.nested:
        sync_session.info["reliable_resumed"].setdefault(transaction.parent, []).extend(contexts)
        return
    for context in contexts:
        logger.info("reliable %s %r", context["event"], context, extra=context)


def _drop_resumed(sync_session):
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    sync_session.info["reliable_resumed"].pop(transaction, None)


class Outbox:
    def __init__(
        self,
        targets: Mapping[str, TargetConfig],
        adapters: Mapping[str, ProtocolAdapter],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifecycles=None,
    ):
        self.targets = targets
        self.adapters = adapters
        self.clock = clock
        self.lifecycles = lifecycles or {}

    async def enqueue(self, session: AsyncSession, draft: MessageDraft) -> EnqueueResult:
        if not isinstance(draft, MessageDraft) or not isinstance(draft.request_raw, bytes):
            raise InvalidMessage("draft requires raw bytes")
        for value in (draft.target, draft.operation, draft.operation_id):
            if not isinstance(value, str) or "\x00" in value:
                raise InvalidMessage("identity must be a storage-compatible string")
        if draft.business_key is not None and (
            not isinstance(draft.business_key, str) or "\x00" in draft.business_key
        ):
            raise InvalidMessage("invalid business key")
        query = select(ReliableMessage).where(
            ReliableMessage.target == draft.target,
            ReliableMessage.operation == draft.operation,
            ReliableMessage.operation_id == draft.operation_id,
        )
        existing = await session.scalar(query.execution_options(populate_existing=True))
        if existing is not None:
            return self._existing(existing, draft)
        if draft.ttl is not None and (
            not isinstance(draft.ttl, timedelta) or draft.ttl < timedelta(0)
        ):
            raise InvalidMessage("invalid ttl")
        now = utc_datetime(self.clock())
        try:
            expires_at = (
                utc_datetime(draft.expires_at)
                if draft.expires_at is not None
                else (now + draft.ttl if draft.ttl is not None else None)
            )
            message = OutgoingMessage(
                draft.target,
                draft.operation,
                draft.operation_id,
                draft.request_raw,
                draft.business_key,
                None if get_trace_id() == "-" else get_trace_id(),
                now,
                expires_at,
                draft.delivery_snapshot,
            )
            target = self.targets[draft.target].effective(draft.delivery_snapshot)
            request = self.adapters[target.protocol].build_request(message, target)
            if request.body != message.request_raw:
                raise ValueError("adapter changed frozen body")
            validate_request(target.http_config(), request)
        except Exception:
            raise InvalidMessage("invalid new outgoing message") from None
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        values = dict(
            delivery_snapshot=asdict(draft.delivery_snapshot) if draft.delivery_snapshot else None,
            target=message.target,
            operation=message.operation,
            operation_id=message.operation_id,
            request_raw=message.request_raw,
            status=MessageStatus.PENDING,
            attempts=0,
            next_attempt_at=now,
            expires_at=expires_at,
            business_key=message.business_key,
            trace_id=message.trace_id,
            created_at=now,
            updated_at=now,
        )
        while True:
            message_id = await session.scalar(
                insert(ReliableMessage)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["target", "operation", "operation_id"])
                .returning(ReliableMessage.id)
            )
            if message_id is not None:
                return EnqueueResult(message_id, MessageStatus.PENDING, message.business_key)
            existing = await session.scalar(query.execution_options(populate_existing=True))
            if existing is not None:
                return self._existing(existing, draft)

    def _existing(self, existing, draft) -> EnqueueResult:
        snapshot = asdict(draft.delivery_snapshot) if draft.delivery_snapshot else None
        if existing.delivery_snapshot != snapshot:
            raise DuplicateMessage("identity already has different delivery snapshot")
        if existing.request_raw != draft.request_raw and not self._same_request(existing, draft):
            raise DuplicateMessage("identity already has different bytes")
        return EnqueueResult(existing.id, MessageStatus(existing.status), existing.business_key)

    def _same_request(self, existing, draft) -> bool:
        """字节不同但适配器认定内容等价(例如只有时间戳变了)时,同身份重入返回原消息。"""
        if draft.delivery_snapshot is not None:
            return False
        target = self.targets.get(draft.target)
        adapter = self.adapters.get(target.protocol) if target is not None else None
        return isinstance(adapter, ComparableRequests) and adapter.same_request(
            existing.request_raw, draft.request_raw
        )

    async def resume(
        self, session: AsyncSession, target: str, operation: str, operation_id: str
    ) -> bool:
        """STALLED → PENDING,保持原身份与字节;不清除未知事实。非 STALLED 返回 False。"""
        now = utc_datetime(self.clock())
        row = (
            await session.execute(
                update(ReliableMessage)
                .where(
                    ReliableMessage.target == target,
                    ReliableMessage.operation == operation,
                    ReliableMessage.operation_id == operation_id,
                    ReliableMessage.status == MessageStatus.STALLED,
                )
                .values(
                    status=MessageStatus.PENDING,
                    not_received_count=0,
                    next_attempt_at=now,
                    updated_at=now,
                )
                .returning(ReliableMessage.trace_id)
            )
        ).first()
        if row is None:
            return False
        context = {
            "target": target,
            "operation": operation,
            "operation_id": operation_id,
            "message_trace_id": row.trace_id,
            "from_status": MessageStatus.STALLED,
            "to_status": MessageStatus.PENDING,
            "event": "resumed",
        }
        self._log_after_commit(session, context)
        return True

    async def settle(
        self, session: AsyncSession, target: str, operation: str, operation_id: str
    ) -> bool:
        """未结束(PENDING/SENDING/STALLED)→ DONE:对端已通过其他渠道(如业务回调)确认接收。

        不再发送,也不写响应(`response_raw` 为空以区别于收到对端应答的 DONE);正在发送的租约随之
        失效,迟到的发送结果会被忽略。已处于终态返回 False。
        """
        snapshot_changed = await self._snapshot_transition(
            session, target, operation, operation_id, "DONE"
        )
        if snapshot_changed is not None:
            return snapshot_changed
        now = utc_datetime(self.clock())
        row = (
            await session.execute(
                update(ReliableMessage)
                .where(
                    ReliableMessage.target == target,
                    ReliableMessage.operation == operation,
                    ReliableMessage.operation_id == operation_id,
                    ReliableMessage.status.in_(
                        (
                            MessageStatus.PENDING,
                            MessageStatus.SENDING,
                            MessageStatus.STALLED,
                            MessageStatus.HELD,
                        )
                    ),
                )
                .values(
                    status=MessageStatus.DONE,
                    lease_id=None,
                    lease_until=None,
                    next_attempt_at=now,  # STALLED 行没有排期,而约束要求非 STALLED 必须有
                    updated_at=now,
                )
                .returning(ReliableMessage.trace_id)
            )
        ).first()
        if row is None:
            return False
        self._log_after_commit(
            session,
            {
                "target": target,
                "operation": operation,
                "operation_id": operation_id,
                "message_trace_id": row.trace_id,
                "to_status": MessageStatus.DONE,
                "event": "settled",
            },
        )
        return True

    async def hold(self, session, target, operation, operation_id, *, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("pause reason required")
        snapshot_changed = await self._snapshot_transition(
            session, target, operation, operation_id, "HELD", reason
        )
        if snapshot_changed is not None:
            return snapshot_changed
        row = await session.scalar(
            update(ReliableMessage)
            .where(
                ReliableMessage.target == target,
                ReliableMessage.operation == operation,
                ReliableMessage.operation_id == operation_id,
                ReliableMessage.status.in_(("PENDING", "SENDING", "STALLED", "HELD")),
            )
            .values(
                status="HELD",
                pause_reason=reason,
                next_attempt_at=None,
                lease_id=None,
                lease_until=None,
                updated_at=utc_datetime(self.clock()),
            )
            .returning(ReliableMessage.id)
        )
        return row is not None

    async def _snapshot_transition(
        self, session, target, operation, operation_id, status, reason=None
    ):
        from .lifecycle import ensure_savepoint_transaction, transaction_limits
        from .store import AttemptOutcome, Claim
        from .types import DeliverySnapshot

        query = select(ReliableMessage).where(
            ReliableMessage.target == target,
            ReliableMessage.operation == operation,
            ReliableMessage.operation_id == operation_id,
        )
        existing = await session.scalar(query)
        if existing is None or existing.delivery_snapshot is None:
            return None
        snapshot = DeliverySnapshot(**existing.delivery_snapshot)
        port = self.lifecycles.get(snapshot.protocol)
        if port is None:
            raise InvalidMessage("snapshot mutation requires its transaction lifecycle")
        await ensure_savepoint_transaction(session)
        async with session.begin_nested():
            await transaction_limits(session)
            context = await port.lock_context(session, operation_id)
            row = await session.scalar(
                query.with_for_update().execution_options(populate_existing=True)
            )
            if row is None or row.status not in ("PENDING", "SENDING", "STALLED", "HELD"):
                return False
            if row.delivery_snapshot != asdict(snapshot):
                raise InvalidMessage("snapshot changed during transaction")
            now = utc_datetime(self.clock())
            message = OutgoingMessage(
                row.target,
                row.operation,
                row.operation_id,
                row.request_raw,
                row.business_key,
                row.trace_id,
                utc_datetime(row.created_at, known_sqlite_utc=True),
                utc_datetime(row.expires_at, known_sqlite_utc=True),
                snapshot,
            )
            claim = Claim(
                row.id,
                message,
                row.lease_id or "",
                utc_datetime(row.lease_until, known_sqlite_utc=True) or now,
                row.attempts + 1,
                row.not_received_count,
                bool(row.delivery_uncertain),
            )
            row.status = status
            row.pause_reason = reason
            row.lease_id = row.lease_until = None
            row.next_attempt_at = None if status == "HELD" else now
            row.updated_at = now
            await port.apply_transition(
                session, context, claim, AttemptOutcome(MessageStatus(status), pause_reason=reason)
            )
            await session.flush()
        return True

    @staticmethod
    def _log_after_commit(session: AsyncSession, context: dict) -> None:
        # 与其它状态转换一致:只在事务真正提交后记日志,回滚则丢弃。
        sync_session = session.sync_session
        if "reliable_resumed" not in sync_session.info:
            sync_session.info["reliable_resumed"] = {}
            event.listen(sync_session, "after_commit", _log_resumed)
            event.listen(sync_session, "after_rollback", _drop_resumed)
        transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
        sync_session.info["reliable_resumed"].setdefault(transaction, []).append(context)
