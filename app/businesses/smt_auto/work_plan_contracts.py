"""Closed work-plan response unions; no defaults for executable parameters."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.businesses._components.sorting_task.contracts import Face, Identifier

Number = Annotated[float, Field(allow_inf_nan=False)]


class Data(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    @field_validator("bin_type", mode="before", check_fields=False)
    @classmethod
    def integer_bin_type(cls, value):
        if type(value) is not int:
            raise ValueError("bin_type must be a JSON integer")
        return value


class NoWork(Data):
    result: Literal["NO_WORK"]


class Wait(Data):
    result: Literal["WAIT"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


class BinReady(Data):
    result: Literal["READY"]
    cell_id: Identifier
    cell_index: Annotated[int, Field(ge=1, le=7)]
    bin_type: Literal[2, 3, 6]
    reel_layer: int
    reel_thickness: Number
    reel_diameter: Number


class RackReady(Data):
    result: Literal["READY"]
    rack_id: Identifier
    rack_face: Face
    slot_id: Identifier
    rack_layer: int
    rack_column: int
    reel_thickness: Number
    reel_diameter: Number


class InboundSource(Data):
    rack_id: Identifier
    location_code: Identifier
    bin_code: Identifier
    bin_location: str
    cell_id: Identifier
    bin_type: Literal[2, 3, 6]
    cell_index: Annotated[int, Field(ge=1, le=7)]
    reel_layer: int
    reel_thickness: Number
    reel_diameter: Number
    reel_totalthickness: Number


class InboundReady(Data):
    result: Literal["READY"]
    source: InboundSource


DECISIONS = {
    name: TypeAdapter(Annotated[ready | Wait | NoWork, Field(discriminator="result")])
    for name, ready in (("inbound", InboundReady), ("bin", BinReady), ("rack", RackReady))
}
