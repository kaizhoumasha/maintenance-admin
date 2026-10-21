from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import uuid7
from app.models.transport import TransportTask
from app.reliable import MessageDraft, Outbox

from .protocol import DEFAULT_MOVE_TEMPLATE, OPERATION
from .types import (
    BinExchangePair,
    BinMove,
    IdempotencyConflict,
    InvalidTransportRequest,
    Rack,
    RackLocation,
    RackPosition,
    RackState,
    RackStateReader,
    RackStateUnavailable,
    TransportCaller,
    TransportHandle,
    require_uuid7,
)
from .wire import (
    bin_faces,
    bin_move_data,
    exchange_bins_data,
    freeze_request,
    input_digest,
    move_rack_data,
    rotate_rack_data,
    rotate_rack_input,
    timestamp_ms,
)


class TransportService:
    def __init__(
        self,
        outbox: Outbox,
        *,
        target: str = "wms",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid7: Callable[[], UUID] | None = None,
        state_reader: RackStateReader | None = None,
    ):
        self.outbox, self.target = outbox, target
        self.clock, self.uuid7, self.state_reader = clock, uuid7, state_reader

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
        rcs_template_id: str = DEFAULT_MOVE_TEMPLATE,
    ) -> TransportHandle:
        """登记义务,不提交调用方事务;返回值不证明接纳或物理完成。"""
        data = move_rack_data(rack_id, source, target, target_face, rcs_template_id)
        return await self._register(session, "move_rack", client_request_id, caller, data)

    async def rotate_rack(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        rack_id: str,
        position: Rack | RackPosition,
        target_face: str,
        rcs_template_id: str,
    ) -> TransportHandle:
        """原地换面;需要可信的当前精确位置与当前面,缺失即失败关闭。"""
        spec = rotate_rack_input(rack_id, position, target_face, rcs_template_id)

        async def resolve() -> dict:
            return rotate_rack_data(spec, await self._state(rack_id))

        return await self._register(
            session, "rotate_rack", client_request_id, caller, spec, resolve
        )

    async def move_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        moves: list[BinMove],
    ) -> TransportHandle:
        data = bin_move_data(moves)
        return await self._register(
            session, "move_bins", client_request_id, caller, data, self._faces_resolver(data)
        )

    async def exchange_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        exchange_pairs: list[BinExchangePair],
    ) -> TransportHandle:
        data = exchange_bins_data(exchange_pairs)
        return await self._register(
            session, "exchange_bins", client_request_id, caller, data, self._faces_resolver(data)
        )

    async def _state(self, rack_id: str) -> RackState:
        state = await self.state_reader.rack_state(rack_id) if self.state_reader else None
        if not isinstance(state, RackState):
            raise RackStateUnavailable("trusted rack state is unavailable")
        return state

    def _faces_resolver(self, data: dict) -> Callable[[], Awaitable[dict]]:
        async def resolve() -> dict:
            for rack_id, face in bin_faces(data).items():
                if (await self._state(rack_id)).face != face:
                    raise InvalidTransportRequest("rack face does not match the trusted face")
            return data

        return resolve

    async def _register(
        self,
        session: AsyncSession,
        method: str,
        client_request_id: str,
        caller: TransportCaller,
        spec: dict,
        resolve: Callable[[], Awaitable[dict]] | None = None,
    ) -> TransportHandle:
        """四个方法共用的登记流程:重放不读可信状态,新请求先核对状态再冻结并入队。"""
        require_uuid7(client_request_id)
        if not isinstance(caller, TransportCaller):
            raise InvalidTransportRequest("caller must be a TransportCaller")
        digest = input_digest(method, caller, spec)
        query = (
            select(TransportTask)
            .where(TransportTask.client_request_id == client_request_id)
            .execution_options(populate_existing=True)
        )
        existing = await session.scalar(query)
        if existing is not None:
            return self._existing(existing, digest)
        data = await resolve() if resolve else spec
        now = self.clock()
        timestamp = timestamp_ms(now)
        new_id = self.uuid7 or (lambda: uuid7(now))
        operation_id = str(new_id())
        task_id = "transport-" + str(new_id())
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
                    kind=data["kind"],
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
