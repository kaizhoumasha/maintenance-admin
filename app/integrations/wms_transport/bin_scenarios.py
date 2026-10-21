from sqlalchemy.ext.asyncio import AsyncSession

from .service import TransportService, transport_service
from .types import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    InvalidTransportRequest,
    RackBinSlot,
    TransportCaller,
    TransportHandle,
)


def _directed(moves: list[BinMove], source: type, target: type) -> list[BinMove]:
    """场景固定搬运方向;成员数量、重复与同货架同面等合同规则仍由能力层校验。"""
    if not isinstance(moves, list | tuple) or not moves:
        raise InvalidTransportRequest("expected a nonempty list of BinMove")
    for move in moves:
        if not (
            isinstance(move, BinMove)
            and isinstance(move.source, source)
            and isinstance(move.target, target)
        ):
            raise InvalidTransportRequest(
                f"every move must go from {source.__name__} to {target.__name__}"
            )
    return list(moves)


class BinScenarios:
    """料箱搬运业务接入层:投料、回架、交换。

    无状态:每次调用各自使用调用方传入的会话和调用方身份,并由调用方提交或回滚;
    没有跨步骤的生命周期,所以不需要句柄。选箱、选储位和批次划分由调用方先决定。
    """

    def __init__(self, transport: TransportService):
        self.transport = transport

    async def feed_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        moves: list[BinMove],
    ) -> TransportHandle:
        """投料:料箱从货架储位搬到滚筒线入料位置(合同 §8)。"""
        return await self.transport.move_bins(
            session,
            client_request_id=client_request_id,
            caller=caller,
            moves=_directed(moves, RackBinSlot, HandoffPosition),
        )

    async def return_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        moves: list[BinMove],
    ) -> TransportHandle:
        """回架:料箱从滚筒线出料位置搬回货架储位(合同 §8)。"""
        return await self.transport.move_bins(
            session,
            client_request_id=client_request_id,
            caller=caller,
            moves=_directed(moves, HandoffPosition, RackBinSlot),
        )

    async def exchange_bins(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        exchange_pairs: list[BinExchangePair],
    ) -> TransportHandle:
        """交换:一个协调任务内交换 1~2 对料箱。"""
        return await self.transport.exchange_bins(
            session,
            client_request_id=client_request_id,
            caller=caller,
            exchange_pairs=exchange_pairs,
        )


# 外部调用的统一入口:无需接线,依赖由 `transport_service` 从配置惰性取得。
bin_scenarios = BinScenarios(transport_service)
