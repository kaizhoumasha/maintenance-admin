"""Coarse sorting commands and their versioned completion evidence."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from app.businesses.bin_types import EcsBinType
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import BinLocation, CellIndex, ReelHeight, Reference


class MoveForwardResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

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
        extra="ignore",
        json_schema_extra={"examples": [{"barcode": "RAW-001"}]},
    )

    barcode: str


class MoveToLocationResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

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


class PutToBinResult(MoveToLocationResult):
    actual_qty: Annotated[int, Field(strict=True, ge=0, le=1)] | None = None

    @staticmethod
    def confirmed_failure_data(data: object) -> bool:
        return isinstance(data, dict) and (
            not data
            or (
                type(data.get("actual_qty")) is int
                and data == {"actual_qty": 0, "pick_and_put_result": "PUT_FAILED"}
            )
        )

    @model_validator(mode="before")
    @classmethod
    def validate_result_data(cls, data, info: ValidationInfo):
        if not isinstance(data, dict):
            return data
        status = (info.context or {}).get("result")
        if status == "SUCCESS" and data.get("pick_and_put_result") == "PUT_FAILED":
            raise ValueError("SUCCESS contradicts PUT_FAILED")
        if data.get("actual_qty") == 0 and (
            status != "FAILED" or not cls.confirmed_failure_data(data)
        ):
            raise ValueError("zero quantity requires FAILED with explicit non-placement")
        return data


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
    model_config = ConfigDict(strict=True, extra="ignore")

    barcode: str
    reason_code: Reference


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
    model_config = ConfigDict(strict=True, extra="ignore")

    barcode: Annotated[str, Field(min_length=1)]
    bin_location: BinLocation
    bin_cell_location: CellIndex
    bin_type: EcsBinType
    reel_totalthickness: ReelHeight
