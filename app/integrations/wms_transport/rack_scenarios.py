from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from .service import TransportService, transport_service
from .types import (
    InvalidTransportRequest,
    Rack,
    RackPosition,
    TransportCaller,
    TransportHandle,
    Zone,
)


class RackRole(StrEnum):
    TRANSFER = "TRANSFER"
    RETURN = "RETURN"
    FIVE_LAYER = "FIVE_LAYER"


type RackDestination = RackPosition | Zone

_TEMPLATES = {
    RackRole.TRANSFER: {"enter": "F01", "rotate": "CK04", "depart": "F01"},
    RackRole.RETURN: {"enter": "F01", "rotate": "CK04", "depart": "F01"},
    RackRole.FIVE_LAYER: {"enter": "CTU01", "rotate": "CTU02", "depart": "CTU03"},
}


def _template(role: RackRole, action: str) -> str:
    if not isinstance(role, RackRole):
        raise InvalidTransportRequest("expected a RackRole")
    return _TEMPLATES[role][action]


@dataclass(frozen=True, slots=True)
class RackCycle:
    """一个货架的一次完整生命周期:进场 → [换面…] → 离场。

    角色只在创建时给一次,三步共用,所以进离场模板的配对由构造保证,不会中途换角色。
    句柄不可变、不持有会话和任何可变状态:同一进程里可以同时存在任意多个货架、不同角色的
    句柄,也可以被多个并发调用方共享;每一步各自使用调用方传入的会话,并由调用方提交或回滚。
    本类不强制步骤先后(那需要持久化的生命周期状态,属于后续阶段)。
    """

    transport: TransportService
    role: RackRole
    rack_id: str
    caller: TransportCaller

    def __post_init__(self):
        _template(self.role, "enter")  # 创建时即拒绝未知角色

    async def enter(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        target: RackDestination,
        target_face: str | None = None,
    ) -> TransportHandle:
        return await self._move("enter", session, client_request_id, target, target_face)

    async def depart(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        target: RackDestination,
        target_face: str | None = None,
    ) -> TransportHandle:
        return await self._move("depart", session, client_request_id, target, target_face)

    async def rotate(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        position: RackPosition,
        target_face: str,
    ) -> TransportHandle:
        return await self.transport.rotate_rack(
            session,
            client_request_id=client_request_id,
            caller=self.caller,
            rack_id=self.rack_id,
            position=position,
            target_face=target_face,
            rcs_template_id=_template(self.role, "rotate"),
        )

    async def _move(
        self,
        action: str,
        session: AsyncSession,
        client_request_id: str,
        target: RackDestination,
        target_face: str | None,
    ) -> TransportHandle:
        if not isinstance(target, RackPosition | Zone):
            raise InvalidTransportRequest("expected a RackPosition or Zone destination")
        return await self.transport.move_rack(
            session,
            client_request_id=client_request_id,
            caller=self.caller,
            rack_id=self.rack_id,
            source=Rack(self.rack_id),
            target=target,
            target_face=target_face,
            rcs_template_id=_template(self.role, action),
        )


class RackScenarios:
    """货架搬运业务接入层入口:为每个货架创建一次生命周期句柄。"""

    def __init__(self, transport: TransportService):
        self.transport = transport

    def cycle(self, *, role: RackRole, rack_id: str, caller: TransportCaller) -> RackCycle:
        return RackCycle(self.transport, role, rack_id, caller)


# 外部调用的统一入口:无需接线,依赖由 `transport_service` 从配置惰性取得。
rack_scenarios = RackScenarios(transport_service)
