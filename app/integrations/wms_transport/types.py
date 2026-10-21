from dataclasses import dataclass
from uuid import UUID


class InvalidTransportRequest(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


def validate_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InvalidTransportRequest("expected a nonempty string without NUL")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidTransportRequest("string is not valid UTF-8") from None
    return value


def validate_uuid7(value: object) -> str:
    text = validate_text(value)
    try:
        identity = UUID(text)
    except ValueError:
        raise InvalidTransportRequest("expected a canonical UUIDv7") from None
    if identity.version != 7 or str(identity) != text:
        raise InvalidTransportRequest("expected a lowercase canonical UUIDv7")
    return text


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
