"""The shared drain decision; candidates are not reservations."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation
from app.integrations.wms_transport.types import validate_face

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
RackFace = Annotated[str, AfterValidator(validate_face)]
OPERATION = "workline.return_buffer.drain_rack_decide@v1"


@request_operation(OPERATION, Endpoint.DECISIONS)
class DrainRequest(RequestPayload):
    workline_code: Identifier
    required_slot_count: Annotated[int, Field(ge=1)]


class Candidate(RequestPayload):
    rack_id: Identifier
    rack_face: Annotated[list[RackFace], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_faces(self):
        if len(set(self.rack_face)) != len(self.rack_face):
            raise ValueError("duplicate rack face")
        return self


class Ready(RequestPayload):
    result: Literal["READY"]
    racks: Annotated[list[Candidate], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_racks(self):
        if len({r.rack_id for r in self.racks}) != len(self.racks):
            raise ValueError("duplicate rack")
        return self


class Wait(RequestPayload):
    result: Literal["WAIT"]
    reason_code: Literal["NO_DRAIN_RACK_AVAILABLE"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


DECISION = TypeAdapter(Annotated[Ready | Wait, Field(discriminator="result")])
