"""Original three-field inbound request and final ordered WMS decision."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, TypeAdapter, model_validator

from app.integrations.wms_request import Endpoint, RequestPayload, request_operation
from app.integrations.wms_transport.types import validate_face

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
RackFace = Annotated[str, AfterValidator(validate_face)]
OPERATION = "outbound.bin.inbound_batch@v1"


@request_operation(OPERATION, Endpoint.DECISIONS)
class InboundBatch(RequestPayload):
    task_id: Identifier
    rack_id: Identifier
    rack_face: RackFace


class SourceLocator(RequestPayload):
    type: Literal["RACK_BIN_SLOT"]
    rack_id: Identifier
    rack_face: RackFace
    slot_id: Identifier


class Member(RequestPayload):
    bin_code: Identifier
    source_locator: SourceLocator


class Ready(RequestPayload):
    result: Literal["READY"]
    bins: Annotated[list[Member], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_members(self):
        if len({member.bin_code for member in self.bins}) != len(self.bins) or len(
            {
                (
                    member.source_locator.rack_id,
                    member.source_locator.rack_face,
                    member.source_locator.slot_id,
                )
                for member in self.bins
            }
        ) != len(self.bins):
            raise ValueError("duplicate inbound member or source slot")
        return self


class FaceDone(RequestPayload):
    result: Literal["RACK_FACE_DONE"]


DECISION = TypeAdapter(Annotated[Ready | FaceDone, Field(discriminator="result")])
