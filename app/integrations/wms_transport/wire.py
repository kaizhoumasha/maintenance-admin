import hashlib
import json
from datetime import datetime

from app.core.clock import unix_ms

from .protocol import (
    DEFAULT_MOVE_TEMPLATE,
    MOVE_TEMPLATES,
    OPERATION,
    ROTATE_TEMPLATES,
)
from .types import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    InvalidTransportRequest,
    Rack,
    RackBinSlot,
    RackLocation,
    RackPosition,
    RackState,
    TransportCaller,
    Zone,
    require_uuid7,
    validate_face,
    validate_text,
)

POSITION_KINDS = {Rack: "RACK", Zone: "ZONE", RackPosition: "RACK_POSITION"}


def timestamp_ms(now: datetime) -> int:
    try:
        milliseconds = unix_ms(now)
    except ValueError as error:
        raise InvalidTransportRequest(str(error)) from None
    if not 0 <= milliseconds <= 2**63 - 1:
        raise InvalidTransportRequest("timestamp is outside the protocol range")
    return milliseconds


def _template(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidTransportRequest("unsupported RCS template")
    return value


def position_data(position: RackLocation) -> dict:
    kind = POSITION_KINDS.get(type(position))
    if kind is None:
        raise InvalidTransportRequest("unsupported rack location")
    return {"kind": kind, "location_code": validate_text(position.code)}


def move_rack_data(
    rack_id: str,
    source: RackLocation,
    target: RackLocation,
    target_face: str | None = None,
    rcs_template_id: str = DEFAULT_MOVE_TEMPLATE,
) -> dict:
    validate_text(rack_id)
    _template(rcs_template_id, MOVE_TEMPLATES)
    source_data, target_data = position_data(source), position_data(target)
    if source_data == target_data:
        raise InvalidTransportRequest("source and target must differ")
    for position in (source_data, target_data):
        if position["kind"] == "RACK" and position["location_code"] != rack_id:
            raise InvalidTransportRequest("rack reference must match rack_id")
    data = {
        "kind": "RACK_MOVE",
        "rack_id": rack_id,
        "rcs_template_id": rcs_template_id,
        "source": source_data,
        "target": target_data,
    }
    if target_face is not None:
        data["target_face"] = validate_face(target_face)
    return data


def input_digest(method: str, caller: TransportCaller, data: dict) -> str:
    """对方法名、调用方与规范化入参取摘要;可信状态派生的字段不属于入参。"""
    normalized = {
        "method": method,
        "caller": {"workline_id": caller.workline_id, "station_id": caller.station_id},
        "data": data,
    }
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expect(value: object, allowed: tuple[type, ...], message: str) -> object:
    if not isinstance(value, allowed):
        raise InvalidTransportRequest(message)
    return value


def rotate_rack_input(
    rack_id: str,
    position: Rack | RackPosition,
    target_face: str,
    rcs_template_id: str,
) -> dict:
    """货架原地换面的规范化入参(不含可信状态);合同 §3.2、§3.3。"""
    validate_text(rack_id)
    _template(rcs_template_id, ROTATE_TEMPLATES)
    _expect(position, (Rack, RackPosition), "rotate position must be Rack or RackPosition")
    source = position_data(position)
    if source["kind"] == "RACK" and source["location_code"] != rack_id:
        raise InvalidTransportRequest("rack reference must match rack_id")
    return {
        "kind": "RACK_ROTATE",
        "rack_id": rack_id,
        "rcs_template_id": rcs_template_id,
        "source": source,
        "target_face": validate_face(target_face),
    }


def rotate_rack_data(spec: dict, state: RackState) -> dict:
    """用可信状态补出精确原点位;目标面必须不同于当前面。"""
    if (
        spec["source"]["kind"] == "RACK_POSITION"
        and spec["source"]["location_code"] != state.position
    ):
        raise InvalidTransportRequest("position does not match the trusted position")
    if spec["target_face"] == state.face:
        raise InvalidTransportRequest("target face equals the current face")
    return {
        "kind": spec["kind"],
        "rack_id": spec["rack_id"],
        "rcs_template_id": spec["rcs_template_id"],
        "source": spec["source"],
        "target": {"kind": "RACK_POSITION", "location_code": state.position},
        "target_face": spec["target_face"],
    }


def bin_location_data(location: RackBinSlot | HandoffPosition) -> dict:
    if isinstance(location, RackBinSlot):
        return {
            "kind": "RACK_BIN_SLOT",
            "rack_id": location.rack_id,
            "rack_face": location.rack_face,
            "slot_id": location.slot_id,
        }
    return {"kind": "HANDOFF_POSITION", "location_code": location.code}


def _sequence(value: object, low: int, high: int, message: str) -> list:
    if not isinstance(value, list | tuple) or not low <= len(value) <= high:
        raise InvalidTransportRequest(message)
    return list(value)


def _slots(moves: list[BinMove]) -> list[RackBinSlot]:
    return [
        place
        for move in moves
        for place in (move.source, move.target)
        if isinstance(place, RackBinSlot)
    ]


def _distinct(items: list, message: str) -> None:
    if len(set(items)) != len(items):
        raise InvalidTransportRequest(message)


def _check_bin_moves(moves: list[BinMove]) -> None:
    """BIN_MOVE 与 BIN_EXCHANGE 共有的成员约束:料箱不重复、同货架同面。"""
    _distinct([move.bin_code for move in moves], "duplicate bin_code")
    faces: dict[str, str] = {}
    for slot in _slots(moves):
        if faces.setdefault(slot.rack_id, slot.rack_face) != slot.rack_face:
            raise InvalidTransportRequest("one rack must use one rack_face")


def _moves_data(kind: str, moves: list[BinMove]) -> dict:
    ordered = sorted(moves, key=lambda move: move.bin_code)
    return {
        "kind": kind,
        "moves": [
            {
                "container_id": move.bin_code,
                "source": bin_location_data(move.source),
                "target": bin_location_data(move.target),
            }
            for move in ordered
        ],
    }


def bin_move_data(moves: list[BinMove]) -> dict:
    moves = _sequence(moves, 1, 4, "BIN_MOVE needs 1..4 moves")
    for move in moves:
        _expect(move, (BinMove,), "expected BinMove")
        if move.source == move.target:
            raise InvalidTransportRequest("source and target must differ")
        if not any(isinstance(place, RackBinSlot) for place in (move.source, move.target)):
            raise InvalidTransportRequest("at least one end must be a RACK_BIN_SLOT")
    _distinct(_slots(moves), "duplicate RACK_BIN_SLOT")
    _check_bin_moves(moves)
    return _moves_data("BIN_MOVE", moves)


def exchange_bins_data(exchange_pairs: list[BinExchangePair]) -> dict:
    pairs = _sequence(exchange_pairs, 1, 2, "BIN_EXCHANGE needs 1..2 pairs")
    moves, locations = [], []
    for pair in pairs:
        _expect(pair, (BinExchangePair,), "expected BinExchangePair")
        if pair.left_bin_code == pair.right_bin_code or pair.left_location == pair.right_location:
            raise InvalidTransportRequest("exchange sides must differ")
        locations += [pair.left_location, pair.right_location]
        moves += [
            BinMove(pair.left_bin_code, pair.left_location, pair.right_location),
            BinMove(pair.right_bin_code, pair.right_location, pair.left_location),
        ]
    _distinct(locations, "duplicate RACK_BIN_SLOT")
    _check_bin_moves(moves)
    groups = {(slot.rack_id, slot.rack_face) for slot in _slots(moves)}
    if len(groups) > 2:
        raise InvalidTransportRequest("exchange may involve at most two rack faces")
    if len(groups) == 2 and any(
        (move.source.rack_id, move.source.rack_face) == (move.target.rack_id, move.target.rack_face)
        for move in moves
    ):
        raise InvalidTransportRequest("with two rack faces every member must cross them")
    return _moves_data("BIN_EXCHANGE", moves)


def bin_faces(data: dict) -> dict[str, str]:
    """请求涉及的货架及其面,供与可信当前面核对。"""
    return {
        place["rack_id"]: place["rack_face"]
        for move in data["moves"]
        for place in (move["source"], move["target"])
        if place["kind"] == "RACK_BIN_SLOT"
    }


def freeze_request(operation_id: str, task_id: str, timestamp: int, data: dict) -> bytes:
    require_uuid7(operation_id)
    validate_text(task_id)
    if type(timestamp) is not int or not 0 <= timestamp <= 2**63 - 1:
        raise InvalidTransportRequest("invalid timestamp")
    envelope = {
        "operation_id": operation_id,
        "operation": OPERATION,
        "timestamp": timestamp,
        "data": {"transport_task_id": task_id, **data},
    }
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > 262144:
        raise InvalidTransportRequest("transport request exceeds 256 KiB")
    return raw
