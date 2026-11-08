"""Coarse sorting commands and their versioned completion evidence."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from app.businesses.bin_types import EcsBinType
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import BinLocation, CellIndex, ReelHeight, Reference


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


class MoveToLocationResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    actual_qty: Annotated[int, Field(ge=1, le=1)] | None = None
    location: Reference | None = None

    @model_validator(mode="after")
    def validate_success(self, info: ValidationInfo) -> Self:
        if (info.context or {}).get("result") == "SUCCESS" and (
            self.actual_qty is None or self.location is None
        ):
            raise ValueError("actual_qty and location must be supplied together")
        return self


class MoveToNgResult(MoveToLocationResult):
    @model_validator(mode="after")
    def validate_evidence_pair(self) -> Self:
        if self.model_fields_set and (self.actual_qty is None or self.location is None):
            raise ValueError("actual_qty and location must be supplied together")
        return self


@ecs_command(
    "MOVE_TO_NG",
    device_roles=("INPUT_ARM",),
    annex_id="coarse_sorting.move_to_ng",
    annex_version="1",
    result=MoveToNgResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class MoveToNg(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    barcode: str
    reason_code: Reference


@ecs_command(
    "PUT_TO_BIN",
    device_roles=("OUTPUT_ARM",),
    annex_id="coarse_sorting.put_to_bin",
    annex_version="1",
    result=MoveToLocationResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class PutToBin(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    barcode: Annotated[str, Field(min_length=1)]
    bin_location: BinLocation
    bin_cell_location: CellIndex
    bin_type: EcsBinType
    reel_totalthickness: ReelHeight
