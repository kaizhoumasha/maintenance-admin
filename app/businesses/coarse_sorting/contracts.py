"""Frozen scan-acceptance payload; business validation and commands are separate stages."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from app.businesses.bin_types import BinType
from app.integrations.ecs_callback import CallbackPayload


class ScanCompletedData(CallbackPayload):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "location": "RS-INPUT-ARM-01",
                    "barcode": "TEST-REEL-001",
                    "scan_time": 1793313931285,
                    "size_inch": 7,
                    "thickness_mm": 15.3,
                    "result": "OK",
                }
            ]
        }
    )

    barcode: str
    size_inch: int | FiniteFloat = Field(description="料盘直径，单位为英寸")
    thickness_mm: int | FiniteFloat = Field(description="料盘厚度，单位为毫米")


NonemptyText = Annotated[str, Field(min_length=1)]


class ValidateBindDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    result: Literal["OK", "NG"]
    binding_id: NonemptyText | None = None
    reason_code: NonemptyText | None = None

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        if self.result == "OK":
            if self.binding_id is None or self.reason_code is not None:
                raise ValueError("OK requires binding_id and forbids reason_code")
        elif self.reason_code is None or self.binding_id is not None:
            raise ValueError("NG requires reason_code and forbids binding_id")
        return self


ALLOCATE = "inbound.coarse_sorting.storage_allocate@v1"
REPORT = "inbound.coarse_sorting.execution_report@v1"

Reference = Annotated[str, StringConstraints(min_length=1, pattern=r"^[^\x00]*[^\s\x00][^\x00]*$")]
BinLocation = Literal["A", "B", "C", "D"]
CellIndex = Annotated[int, Field(ge=1, le=7)]
ReelHeight = Annotated[int, Field(ge=0)] | Annotated[float, Field(ge=0, allow_inf_nan=False)]


class WorklineDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    workline_code: Reference


class Allocated(WorklineDecision):
    result: Literal["ALLOCATED"]
    rack_id: Reference
    bin_code: Reference
    bin_location: BinLocation
    cell_id: Reference
    cell_index: CellIndex
    bin_type: BinType
    reel_totalthickness: ReelHeight


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
