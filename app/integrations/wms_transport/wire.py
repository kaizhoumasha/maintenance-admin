import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .protocol import OPERATION, RCS_TEMPLATES
from .types import (
    InvalidTransportRequest,
    Rack,
    RackLocation,
    RackPosition,
    TransportCaller,
    Zone,
    validate_text,
    validate_uuid7,
)

POSITION_KINDS = {Rack: "RACK", Zone: "ZONE", RackPosition: "RACK_POSITION"}


def timestamp_ms(now: datetime) -> int:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise InvalidTransportRequest("clock must return an aware datetime")
    milliseconds = (now.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(
        milliseconds=1
    )
    if not 0 <= milliseconds <= 2**63 - 1:
        raise InvalidTransportRequest("timestamp is outside the protocol range")
    return milliseconds


def new_uuid7(now: datetime, *, randbits: Callable[[int], int] = secrets.randbits) -> UUID:
    milliseconds = timestamp_ms(now)
    if milliseconds >= 2**48:
        raise InvalidTransportRequest("timestamp is outside the UUIDv7 range")
    random_bits = randbits(74)
    return UUID(
        int=(milliseconds << 80)
        | (7 << 76)
        | ((random_bits >> 62) << 64)
        | (2 << 62)
        | (random_bits & (2**62 - 1))
    )


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
    rcs_template_id: str = "F01",
) -> dict:
    validate_text(rack_id)
    if not isinstance(rcs_template_id, str) or rcs_template_id not in RCS_TEMPLATES:
        raise InvalidTransportRequest("unsupported RCS template")
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
        if len(validate_text(target_face)) > 10:
            raise InvalidTransportRequest("target_face exceeds 10 code points")
        data["target_face"] = target_face
    return data


def input_digest(caller: TransportCaller, data: dict) -> str:
    normalized = {
        "method": "move_rack",
        "caller": {"workline_id": caller.workline_id, "station_id": caller.station_id},
        "data": data,
    }
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def freeze_request(operation_id: str, task_id: str, timestamp: int, data: dict) -> bytes:
    validate_uuid7(operation_id)
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
