from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_settings
from app.core.identifiers import uuid7
from app.models.transport import TransportTask
from app.reliable import MessageDraft, Outbox
from app.reliable.config import parse_reliable_config

from .adapter import ADAPTERS
from .protocol import DEFAULT_MOVE_TEMPLATE, OPERATION, PROTOCOL
from .types import (
    BinExchangePair,
    BinMove,
    IdempotencyConflict,
    InvalidTransportRequest,
    RackLocation,
    RackPosition,
    TransportCaller,
    TransportHandle,
    require_uuid7,
)
from .wire import (
    bin_move_data,
    exchange_bins_data,
    freeze_request,
    input_digest,
    move_rack_data,
    rotate_rack_data,
    timestamp_ms,
)


class TransportService:
    def __init__(
        self,
        outbox: Outbox | None = None,
        *,
        target: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid7: Callable[[], UUID] | None = None,
    ):
        """不传 `outbox` / `target` 时,首次使用从 RELIABLE_TARGETS 配置惰性取得并缓存,
        业务代码无需任何接线;显式参数用于测试和特殊装配。"""
        self._outbox, self._target = outbox, target
        self.clock, self.uuid7 = clock, uuid7

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            targets = parse_reliable_config(get_settings()).targets
            self._outbox = Outbox(targets, ADAPTERS, clock=self.clock)
        return self._outbox

    @property
    def target(self) -> str:
        if self._target is None:
            names = [
                name for name, item in self.outbox.targets.items() if item.protocol == PROTOCOL
            ]
            if not names:
                existing = ", ".join(f"{n}[{t.protocol}]" for n, t in self.outbox.targets.items())
                example = f'{{"targets":{{"{PROTOCOL}":{{"base_url":"http://host:port","protocol":"{PROTOCOL}"}}}}}}'
                raise RuntimeError(
                    f"未配置 WMS Transport 目标:RELIABLE_TARGETS 中没有 protocol={PROTOCOL} "
                    f"的目标(现有:{existing or '无'})。示例:RELIABLE_TARGETS='{example}'"
                )
            if len(names) > 1:
                raise RuntimeError(f"配置了多个 WMS Transport 目标 {names},请显式指定 target")
            self._target = names[0]
        return self._target

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
        position: RackPosition,
        target_face: str,
        rcs_template_id: str,
    ) -> TransportHandle:
        """原地换面;精确位置 `position` 原样写入 source 与 target,位置与当前面由 WMS 校验。"""
        data = rotate_rack_data(rack_id, position, target_face, rcs_template_id)
        return await self._register(session, "rotate_rack", client_request_id, caller, data)

    async def move_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        moves: list[BinMove],
    ) -> TransportHandle:
        data = bin_move_data(moves)
        return await self._register(session, "move_bins", client_request_id, caller, data)

    async def exchange_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        exchange_pairs: list[BinExchangePair],
    ) -> TransportHandle:
        data = exchange_bins_data(exchange_pairs)
        return await self._register(session, "exchange_bins", client_request_id, caller, data)

    async def _register(
        self,
        session: AsyncSession,
        method: str,
        client_request_id: str,
        caller: TransportCaller,
        data: dict,
    ) -> TransportHandle:
        """四个方法共用的登记流程:同号重放返回原任务,新请求冻结字节后同事务入队。"""
        outbox, target = self.outbox, self.target  # 未配置时先于任何校验和写入失败
        require_uuid7(client_request_id)
        if not isinstance(caller, TransportCaller):
            raise InvalidTransportRequest("caller must be a TransportCaller")
        digest = input_digest(method, caller, data)
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
            await outbox.enqueue(
                session,
                MessageDraft(target, OPERATION, operation_id, raw, business_key=task_id),
            )
        return TransportHandle(task_id, client_request_id)

    @staticmethod
    def _existing(task: TransportTask, digest: str) -> TransportHandle:
        if task.input_digest != digest:
            raise IdempotencyConflict("client_request_id already has different input")
        return TransportHandle(task.transport_task_id, task.client_request_id)


# 外部调用的统一入口:无需任何接线,首次使用时从配置惰性取得 Outbox 与目标。
transport_service = TransportService()
