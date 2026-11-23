"""Closed work-plan response unions; no defaults for executable parameters."""

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from app.businesses._components.sorting_task.contracts import Face, Identifier
from app.businesses.bin_types import BinType

Number = Annotated[float, Field(allow_inf_nan=False)]


def legacy_size_inch(diameter):
    """Translate only pre-contract millimeter snapshots for recovery."""
    try:
        diameter_mm = Decimal(str(diameter))
    except InvalidOperation as exc:
        raise ValueError("invalid legacy reel diameter") from exc
    return min((15, 13, 7), key=lambda inches: abs(diameter_mm - Decimal("25.4") * inches))


def size_inch_value(data):
    if "size_inch" in data:
        return data["size_inch"]
    return legacy_size_inch(data["reel_diameter"])


class Data(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def recover_legacy_size(cls, value):
        if (
            isinstance(value, dict)
            and "size_inch" not in value
            and value.get("reel_diameter") is not None
        ):
            value = dict(value)
            value["size_inch"] = legacy_size_inch(value["reel_diameter"])
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
    bin_type: BinType
    reel_layer: int
    reel_thickness: Number
    size_inch: Number
    reel_diameter: Number | None


class RackReady(Data):
    result: Literal["READY"]
    rack_id: Identifier
    rack_face: Face
    slot_id: Identifier
    rack_layer: int
    rack_column: int
    reel_thickness: Number
    size_inch: Number
    reel_diameter: Number | None


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
    size_inch: Number
    reel_diameter: Number | None
    reel_totalthickness: Number


class InboundReady(Data):
    result: Literal["READY"]
    source: InboundSource


DECISIONS = {
    name: TypeAdapter(Annotated[ready | Wait | NoWork, Field(discriminator="result")])
    for name, ready in (("inbound", InboundReady), ("bin", BinReady), ("rack", RackReady))
}
