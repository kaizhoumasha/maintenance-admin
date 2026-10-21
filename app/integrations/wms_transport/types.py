from dataclasses import dataclass
from typing import Protocol

from app.core.identifiers import validate_uuid7


class InvalidTransportRequest(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class RackStateUnavailable(InvalidTransportRequest):
    """可信的货架当前位置/面缺失:失败关闭,不从其它数据推断(合同 §3.2)。"""


def validate_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidTransportRequest("expected a nonempty string without NUL")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidTransportRequest("string is not valid UTF-8") from None
    return value


def validate_face(value: object) -> str:
    """货架面是不透明字符串:1..10 个 code point,无 NUL,原样保存(合同 §5.3)。"""
    if len(validate_text(value)) > 10:
        raise InvalidTransportRequest("face exceeds 10 code points")
    return value


def require_uuid7(value: object) -> str:
    try:
        return validate_uuid7(value)
    except ValueError as error:
        raise InvalidTransportRequest(str(error)) from None


@dataclass(frozen=True)
class TransportCaller:
    workline_id: str
    station_id: str | None = None

    def __post_init__(self):
        validate_text(self.workline_id)
        if self.station_id is not None:
            validate_text(self.station_id)


@dataclass(frozen=True)
class TransportHandle:
    transport_task_id: str
    client_request_id: str


@dataclass(frozen=True)
class Rack:
    code: str

    def __post_init__(self):
        validate_text(self.code)


@dataclass(frozen=True)
class Zone:
    code: str

    def __post_init__(self):
        validate_text(self.code)


@dataclass(frozen=True)
class RackPosition:
    code: str

    def __post_init__(self):
        validate_text(self.code)


type RackLocation = Rack | Zone | RackPosition


@dataclass(frozen=True)
class RackBinSlot:
    rack_id: str
    rack_face: str
    slot_id: str

    def __post_init__(self):
        validate_text(self.rack_id)
        validate_face(self.rack_face)
        validate_text(self.slot_id)


@dataclass(frozen=True)
class HandoffPosition:
    code: str

    def __post_init__(self):
        validate_text(self.code)


type BinLocation = RackBinSlot | HandoffPosition


def _bin_location(value: object) -> BinLocation:
    if not isinstance(value, RackBinSlot | HandoffPosition):
        raise InvalidTransportRequest("expected a RackBinSlot or HandoffPosition")
    return value


@dataclass(frozen=True)
class BinMove:
    bin_code: str
    source: BinLocation
    target: BinLocation

    def __post_init__(self):
        validate_text(self.bin_code)
        _bin_location(self.source)
        _bin_location(self.target)


@dataclass(frozen=True)
class BinExchangePair:
    left_bin_code: str
    left_location: RackBinSlot
    right_bin_code: str
    right_location: RackBinSlot

    def __post_init__(self):
        validate_text(self.left_bin_code)
        validate_text(self.right_bin_code)
        for location in (self.left_location, self.right_location):
            if not isinstance(location, RackBinSlot):
                raise InvalidTransportRequest("exchange locations must be RackBinSlot")


@dataclass(frozen=True)
class RackState:
    """WES 可信的货架当前精确位置与工作面。"""

    position: str
    face: str

    def __post_init__(self):
        validate_text(self.position)
        validate_face(self.face)


class RackStateReader(Protocol):
    async def rack_state(self, rack_id: str) -> RackState | None: ...
