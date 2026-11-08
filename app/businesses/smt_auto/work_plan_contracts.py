"""Closed work-plan response unions; no defaults for executable parameters."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.businesses._components.sorting_task.contracts import Face, Identifier
from app.businesses.bin_types import BinType

Number = Annotated[float, Field(allow_inf_nan=False)]


class Data(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class NoWork(Data):
    result: Literal["NO_WORK"]


class Wait(Data):
    result: Literal["WAIT"]
    retry_after_ms: Annotated[int, Field(ge=1, le=60000)]


class BinReady(Data):
    result: Literal["READY"]
    cell_id: Identifier
    cell_index: Annotated[int, Field(ge=1, le=7)]
    bin_type: BinType
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
    bin_type: BinType
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
