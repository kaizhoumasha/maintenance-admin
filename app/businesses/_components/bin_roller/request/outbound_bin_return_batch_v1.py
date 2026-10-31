"""Strict original FIFO return allocation request and ordered prefix decision."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation
from app.integrations.wms_transport.types import validate_face

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
RackFace = Annotated[str, AfterValidator(validate_face)]

OPERATION = "outbound.bin.return_batch@v1"


class Handoff(RequestPayload):
    type: Literal["HANDOFF_POSITION"]
    location_code: Identifier


class Candidate(RequestPayload):
    sequence_no: Annotated[int, Field(ge=1, le=4)]
    bin_code: Identifier
    source: Handoff


@request_operation(OPERATION, Endpoint.DECISIONS)
class ReturnBatchRequest(RequestPayload):
    workline_code: Identifier
    rack_id: Identifier
    rack_face: RackFace
    return_candidates: Annotated[list[Candidate], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def ordered_candidates(self):
        if (
            [m.sequence_no for m in self.return_candidates]
            != list(range(1, len(self.return_candidates) + 1))
            or len({m.bin_code for m in self.return_candidates}) != len(self.return_candidates)
            or len({m.source.location_code for m in self.return_candidates}) != 1
        ):
            raise ValueError("candidates must be unique ordered bins from one handoff")
        return self


class Target(RequestPayload):
    type: Literal["RACK_BIN_SLOT"]
    rack_id: Identifier
    rack_face: RackFace
    slot_id: Identifier


class Move(RequestPayload):
    sequence_no: Annotated[int, Field(ge=1, le=4)]
    bin_code: Identifier
    target: Target


class Ready(RequestPayload):
    result: Literal["READY"]
    moves: Annotated[list[Move], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def ordered_moves(self):
        if (
            [m.sequence_no for m in self.moves] != list(range(1, len(self.moves) + 1))
            or len({m.bin_code for m in self.moves}) != len(self.moves)
            or len({(m.target.rack_id, m.target.rack_face, m.target.slot_id) for m in self.moves})
            != len(self.moves)
        ):
            raise ValueError("moves must be a unique ordered prefix with distinct targets")
        return self


class NoBatch(RequestPayload):
    result: Literal["NO_BATCH"]


DECISION = TypeAdapter(Annotated[Ready | NoBatch, Field(discriminator="result")])
