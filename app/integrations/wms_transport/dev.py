"""联调控制台的 Transport 场景:扁平请求体 → 领域对象 → 真实的业务接入层(货架生命周期、料箱场景)。

Zone、储位、滚筒线编码的示例是占位值,联调前按现场替换。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dev_scenarios import DevOutcome, DevRequest, dev_scenario
from app.core.errors import ErrorCode

from .bin_scenarios import bin_scenarios
from .rack_scenarios import RackDestination, RackRole, rack_scenarios
from .types import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    IdempotencyConflict,
    InvalidTransportRequest,
    RackBinSlot,
    RackPosition,
    TransportCaller,
    TransportHandle,
    Zone,
)

ERRORS = {
    InvalidTransportRequest: ErrorCode.INVALID_REQUEST,
    IdempotencyConflict: ErrorCode.CONFLICT,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 嵌套字段拼错时报 400,而不是被静默丢弃


class TransportRouteIn(_Strict):
    workline_id: str
    station_id: str | None = None

    def caller(self) -> TransportCaller:
        return TransportCaller(self.workline_id, self.station_id)


class TransportDestinationIn(_Strict):
    kind: Literal["position", "zone"]
    code: str

    def domain(self) -> RackDestination:
        return RackPosition(self.code) if self.kind == "position" else Zone(self.code)


class TransportSlotIn(_Strict):
    rack_id: str
    rack_face: str
    slot_id: str

    def domain(self) -> RackBinSlot:
        return RackBinSlot(self.rack_id, self.rack_face, self.slot_id)


class TransportHandoffIn(_Strict):
    handoff_code: str

    def domain(self) -> HandoffPosition:
        return HandoffPosition(self.handoff_code)


class RackMoveIn(DevRequest, TransportRouteIn):
    role: RackRole
    rack_id: str
    cycle_id: str | None = None  # 同一轮的后续步骤填首步返回任务里的 cycle_id
    target: TransportDestinationIn
    target_face: str | None = None


class RackRotateIn(DevRequest, TransportRouteIn):
    role: RackRole
    rack_id: str
    cycle_id: str | None = None
    position_code: str
    target_face: str


class TransportFeedMoveIn(_Strict):
    bin_code: str
    source: TransportSlotIn
    target: TransportHandoffIn


class BinFeedIn(DevRequest, TransportRouteIn):
    moves: list[TransportFeedMoveIn]


class TransportReturnMoveIn(_Strict):
    bin_code: str
    source: TransportHandoffIn
    target: TransportSlotIn


class BinReturnIn(DevRequest, TransportRouteIn):
    moves: list[TransportReturnMoveIn]


class TransportExchangePairIn(_Strict):
    left_bin_code: str
    left_location: TransportSlotIn
    right_bin_code: str
    right_location: TransportSlotIn


class BinExchangeIn(DevRequest, TransportRouteIn):
    exchange_pairs: list[TransportExchangePairIn]


def _outcome(handle: TransportHandle) -> DevOutcome:
    return DevOutcome(handle.transport_task_id, handle.client_request_id)


_CALLER = {"workline_id": "WL-01", "station_id": "ST-01"}
_RACK_MOVE = {**_CALLER, "role": "TRANSFER", "rack_id": "510004"}
_SLOT = {"rack_id": "510004", "rack_face": "90", "slot_id": "1-1"}


@dev_scenario(
    "rack.enter",
    "货架进场",
    example={**_RACK_MOVE, "target": {"kind": "position", "code": "KT16"}, "target_face": "90"},
    errors=ERRORS,
)
async def rack_enter(session: AsyncSession, req: RackMoveIn) -> DevOutcome:
    cycle = rack_scenarios.cycle(
        role=req.role, rack_id=req.rack_id, caller=req.caller(), cycle_id=req.cycle_id
    )
    return _outcome(
        await cycle.enter(
            session,
            client_request_id=req.request_id(),
            target=req.target.domain(),
            target_face=req.target_face,
        )
    )


@dev_scenario(
    "rack.rotate",
    "货架换面",
    example={**_RACK_MOVE, "position_code": "KT16", "target_face": "180"},
    errors=ERRORS,
)
async def rack_rotate(session: AsyncSession, req: RackRotateIn) -> DevOutcome:
    cycle = rack_scenarios.cycle(
        role=req.role, rack_id=req.rack_id, caller=req.caller(), cycle_id=req.cycle_id
    )
    return _outcome(
        await cycle.rotate(
            session,
            client_request_id=req.request_id(),
            position=RackPosition(req.position_code),
            target_face=req.target_face,
        )
    )


@dev_scenario(
    "rack.depart",
    "货架离场",
    example={**_RACK_MOVE, "target": {"kind": "zone", "code": "ZONE-A"}},
    errors=ERRORS,
)
async def rack_depart(session: AsyncSession, req: RackMoveIn) -> DevOutcome:
    cycle = rack_scenarios.cycle(
        role=req.role, rack_id=req.rack_id, caller=req.caller(), cycle_id=req.cycle_id
    )
    return _outcome(
        await cycle.depart(
            session,
            client_request_id=req.request_id(),
            target=req.target.domain(),
            target_face=req.target_face,
        )
    )


@dev_scenario(
    "bin.feed",
    "料箱投料(货架储位 → 滚筒线入料位)",
    example={
        **_CALLER,
        "moves": [
            {"bin_code": "BIN-001", "source": _SLOT, "target": {"handoff_code": "ROLL-IN-1"}}
        ],
    },
    errors=ERRORS,
)
async def bin_feed(session: AsyncSession, req: BinFeedIn) -> DevOutcome:
    moves = [BinMove(m.bin_code, m.source.domain(), m.target.domain()) for m in req.moves]
    return _outcome(
        await bin_scenarios.feed_bins(
            session, client_request_id=req.request_id(), caller=req.caller(), moves=moves
        )
    )


@dev_scenario(
    "bin.return",
    "料箱回架(滚筒线出料位 → 货架储位)",
    example={
        **_CALLER,
        "moves": [
            {"bin_code": "BIN-001", "source": {"handoff_code": "ROLL-OUT-1"}, "target": _SLOT}
        ],
    },
    errors=ERRORS,
)
async def bin_return(session: AsyncSession, req: BinReturnIn) -> DevOutcome:
    moves = [BinMove(m.bin_code, m.source.domain(), m.target.domain()) for m in req.moves]
    return _outcome(
        await bin_scenarios.return_bins(
            session, client_request_id=req.request_id(), caller=req.caller(), moves=moves
        )
    )


@dev_scenario(
    "bin.exchange",
    "料箱交换(1~2 对)",
    example={
        **_CALLER,
        "exchange_pairs": [
            {
                "left_bin_code": "BIN-001",
                "left_location": _SLOT,
                "right_bin_code": "BIN-002",
                "right_location": {**_SLOT, "slot_id": "1-2"},
            }
        ],
    },
    errors=ERRORS,
)
async def bin_exchange(session: AsyncSession, req: BinExchangeIn) -> DevOutcome:
    pairs = [
        BinExchangePair(
            p.left_bin_code, p.left_location.domain(), p.right_bin_code, p.right_location.domain()
        )
        for p in req.exchange_pairs
    ]
    return _outcome(
        await bin_scenarios.exchange_bins(
            session, client_request_id=req.request_id(), caller=req.caller(), exchange_pairs=pairs
        )
    )
