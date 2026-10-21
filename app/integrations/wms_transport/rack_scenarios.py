from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from .service import TransportService
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


class RackScenarios:
    """无状态场景入口;调用方固定生命周期角色,并持有提交/回滚权。"""

    def __init__(self, transport: TransportService):
        self.transport = transport

    async def enter_rack(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        role: RackRole,
        rack_id: str,
        target: RackDestination,
        target_face: str | None = None,
    ) -> TransportHandle:
        return await self._move(
            "enter",
            session,
            client_request_id=client_request_id,
            caller=caller,
            role=role,
            rack_id=rack_id,
            target=target,
            target_face=target_face,
        )

    async def depart_rack(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        role: RackRole,
        rack_id: str,
        target: RackDestination,
        target_face: str | None = None,
    ) -> TransportHandle:
        return await self._move(
            "depart",
            session,
            client_request_id=client_request_id,
            caller=caller,
            role=role,
            rack_id=rack_id,
            target=target,
            target_face=target_face,
        )

    async def _move(
        self,
        action: str,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        role: RackRole,
        rack_id: str,
        target: RackDestination,
        target_face: str | None,
    ) -> TransportHandle:
        template = _template(role, action)
        if not isinstance(target, RackPosition | Zone):
            raise InvalidTransportRequest("expected a RackPosition or Zone destination")
        return await self.transport.move_rack(
            session,
            client_request_id=client_request_id,
            caller=caller,
            rack_id=rack_id,
            source=Rack(rack_id),
            target=target,
            target_face=target_face,
            rcs_template_id=template,
        )

    async def rotate_rack(
        self,
        session: AsyncSession,
        *,
        client_request_id: str,
        caller: TransportCaller,
        role: RackRole,
        rack_id: str,
        target_face: str,
    ) -> TransportHandle:
        template = _template(role, "rotate")
        return await self.transport.rotate_rack(
            session,
            client_request_id=client_request_id,
            caller=caller,
            rack_id=rack_id,
            position=Rack(rack_id),
            target_face=target_face,
            rcs_template_id=template,
        )
