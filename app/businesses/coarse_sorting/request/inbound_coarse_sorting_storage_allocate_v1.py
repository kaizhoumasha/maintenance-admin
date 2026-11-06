"""Candidate allocation contract. Formal registration awaits WMS signoff."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from app.integrations.wms_request import RequestPayload

OPERATION = "inbound.coarse_sorting.storage_allocate@v1"
Reference = Annotated[str, StringConstraints(min_length=1, pattern=r"^[^\x00]*[^\s\x00][^\x00]*$")]
BinLocation = Literal["1", "2", "3", "4"]
BinType = Literal["3", "6"]
ReelHeight = Annotated[int, Field(ge=0)] | Annotated[float, Field(ge=0, allow_inf_nan=False)]


class StorageAllocate(RequestPayload):
    workline_code: Reference
    binding_id: Reference


class WorklineDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    workline_code: Reference


class Allocated(WorklineDecision):
    result: Literal["ALLOCATED"]
    rack_id: Reference
    bin_code: Reference
    bin_location: BinLocation
    cell_id: Reference
    bin_type: BinType
    reel_totalthickness: ReelHeight

    @model_validator(mode="after")
    def validate_cell(self) -> Self:
        if self.cell_id[-1] not in "123456"[: int(self.bin_type)]:
            raise ValueError("cell_id suffix is outside the bin type")
        return self


class Wait(WorklineDecision):
    result: Literal["WAIT"]


class CurrentRack(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    rack_id: Reference


class IncomingRack(CurrentRack):
    target_face: Annotated[Reference, StringConstraints(max_length=10)]


class Replenish(WorklineDecision):
    result: Literal["REPLENISH_REQUIRED"]
    incoming_rack: IncomingRack


class Depart(WorklineDecision):
    result: Literal["DEPART_REQUIRED"]
    current_rack: CurrentRack


class Replace(WorklineDecision):
    result: Literal["REPLACE_REQUIRED"]
    current_rack: CurrentRack
    incoming_rack: IncomingRack


allocation_decision = TypeAdapter(
    Annotated[Allocated | Wait | Replenish | Depart | Replace, Field(discriminator="result")]
)
