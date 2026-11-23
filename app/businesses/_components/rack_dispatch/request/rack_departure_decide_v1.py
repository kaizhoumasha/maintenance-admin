"""Confirmed shared four-field departure decision."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation
from app.integrations.wms_transport.types import validate_face

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
RackFace = Annotated[str, AfterValidator(validate_face)]
OPERATION = "rack.departure_decide@v1"


class Position(RequestPayload):
    type: Literal["RACK_POSITION"]
    location_code: Identifier


class Destination(RequestPayload):
    type: Literal["ZONE", "RACK_POSITION"]
    location_code: Identifier


@request_operation(OPERATION, Endpoint.DECISIONS)
class DepartureRequest(RequestPayload):
    workline_code: Identifier
    rack_id: Identifier
    current_location: Position
    current_face: RackFace | None


class Ready(RequestPayload):
    result: Literal["READY"]
    rack_destination: Destination


class Wait(RequestPayload):
    result: Literal["WAIT"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


DECISION = TypeAdapter(Annotated[Ready | Wait, Field(discriminator="result")])
