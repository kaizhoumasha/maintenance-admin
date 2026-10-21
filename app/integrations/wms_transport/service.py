from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.models.transport import TransportTask
from app.reliable import MessageDraft, Outbox

from .protocol import OPERATION
from .types import (
    IdempotencyConflict,
    InvalidTransportRequest,
    RackLocation,
    TransportCaller,
    TransportHandle,
    require_uuid7,
)
from .wire import freeze_request, input_digest, move_rack_data, timestamp_ms


class TransportService:
    def __init__(
        self,
        outbox: Outbox,
        *,
        target: str = "wms",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid7: Callable[[], UUID] | None = None,
        uuid4: Callable[[], UUID] = uuid4,
    ):
        self.outbox, self.target = outbox, target
        self.clock, self.uuid7, self.uuid4 = clock, uuid7, uuid4

    async def move_rack(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        rack_id: str,
        source: RackLocation,
        target: RackLocation,
        target_face: str | None = None,
        rcs_template_id: str = "F01",
    ) -> TransportHandle:
        """登记义务,不提交调用方事务;返回值不证明接纳或物理完成。"""
        require_uuid7(client_request_id)
        if not isinstance(caller, TransportCaller):
            raise InvalidTransportRequest("caller must be a TransportCaller")
        data = move_rack_data(rack_id, source, target, target_face, rcs_template_id)
        digest = input_digest(caller, data)
        query = (
            select(TransportTask)
            .where(TransportTask.client_request_id == client_request_id)
            .execution_options(populate_existing=True)
        )
        existing = await session.scalar(query)
        if existing is not None:
            return self._existing(existing, digest)
        now = self.clock()
        timestamp = timestamp_ms(now)
        operation_id = str(self.uuid7() if self.uuid7 is not None else uuid7(now))
        task_uuid = self.uuid4()
        if not isinstance(task_uuid, UUID) or task_uuid.version != 4:
            raise InvalidTransportRequest("task identity factory must return UUIDv4")
        task_id = "transport-" + str(task_uuid)
        raw = freeze_request(operation_id, task_id, timestamp, data)
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        async with session.begin_nested():
            task_row_id = await session.scalar(
                insert(TransportTask)
                .values(
                    client_request_id=client_request_id,
                    transport_task_id=task_id,
                    operation_id=operation_id,
                    kind="RACK_MOVE",
                    input_digest=digest,
                    workline_id=caller.workline_id,
                    station_id=caller.station_id,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["client_request_id"])
                .returning(TransportTask.id)
            )
            if task_row_id is None:
                winner = await session.scalar(query)
                if winner is None:
                    raise RuntimeError("client_request_id conflict without a visible task")
                return self._existing(winner, digest)
            await self.outbox.enqueue(
                session,
                MessageDraft(self.target, OPERATION, operation_id, raw, business_key=task_id),
            )
        return TransportHandle(task_id, client_request_id)

    @staticmethod
    def _existing(task: TransportTask, digest: str) -> TransportHandle:
        if task.input_digest != digest:
            raise IdempotencyConflict("client_request_id already has different input")
        return TransportHandle(task.transport_task_id, task.client_request_id)
