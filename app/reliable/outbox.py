from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_trace_id
from app.http_client import validate_request
from app.models.reliable import ReliableMessage

from .config import TargetConfig
from .store import utc_datetime
from .types import (
    DuplicateMessage,
    EnqueueResult,
    InvalidMessage,
    MessageDraft,
    MessageStatus,
    OutboundMessage,
    ProtocolAdapter,
)


class Outbox:
    def __init__(
        self,
        targets: Mapping[str, TargetConfig],
        adapters: Mapping[str, ProtocolAdapter],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.targets = targets
        self.adapters = adapters
        self.clock = clock

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
            expires_at = now + draft.ttl if draft.ttl is not None else None
            message = OutboundMessage(
                draft.target,
                draft.operation,
                draft.operation_id,
                draft.request_raw,
                draft.business_key,
                None if get_trace_id() == "-" else get_trace_id(),
                now,
                expires_at,
            )
            target = self.targets[draft.target]
            request = self.adapters[target.protocol].build_request(message, target)
            if request.body != message.request_raw:
                raise ValueError("adapter changed frozen body")
            validate_request(target.http_config(), request)
        except Exception:
            raise InvalidMessage("invalid new outbound message") from None
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        values = dict(
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
                return EnqueueResult(message_id, MessageStatus.PENDING)
            existing = await session.scalar(query.execution_options(populate_existing=True))
            if existing is not None:
                return self._existing(existing, draft)

    @staticmethod
    def _existing(existing, draft) -> EnqueueResult:
        if existing.request_raw != draft.request_raw:
            raise DuplicateMessage("identity already has different bytes")
        return EnqueueResult(existing.id, MessageStatus(existing.status))
