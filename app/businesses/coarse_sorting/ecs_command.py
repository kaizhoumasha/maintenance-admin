"""The original conveyor move and its versioned completion evidence."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import BinLocation, BinType, ReelHeight, Reference


class MoveForwardResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # FAILED may have empty data; the business consumer requires quantity for SUCCESS.
    actual_qty: Annotated[int, Field(ge=1, le=1)] | None = None


@ecs_command(
    "MOVE_FORWARD",
    device_roles=("CONVEYOR",),
    annex_id="coarse_sorting.move_forward",
    annex_version="2",
    result=MoveForwardResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class MoveForward(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        json_schema_extra={"examples": [{"barcode": "RAW-001"}]},
    )

    barcode: str


class PutToBinResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    actual_qty: Annotated[int, Field(ge=1, le=1)] | None = None
    location: Reference | None = None


@ecs_command(
    "PUT_TO_BIN",
    device_roles=("OUTPUT_ARM",),
    annex_id="coarse_sorting.put_to_bin",
    annex_version="1",
    result=PutToBinResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class PutToBin(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    barcode: Annotated[str, Field(min_length=1)]
    bin_location: BinLocation
    bin_cell_location: Literal["1", "2", "3", "4", "5", "6"]
    bin_type: BinType
    reel_totalthickness: ReelHeight

    @model_validator(mode="after")
    def validate_cell(self) -> Self:
        if int(self.bin_cell_location) > int(self.bin_type):
            raise ValueError("bin_cell_location is outside the bin type")
        return self
