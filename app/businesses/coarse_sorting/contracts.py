"""Frozen scan-acceptance payload; business validation and commands are separate stages."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    IPvAnyAddress,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from app.businesses.bin_types import BinType
from app.core.strict_json import canonical_digest
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


VALIDATE = "inbound.coarse_sorting.validate_bind@v1"
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


class _MockRoundBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    round_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
    ecs_source_ips: Annotated[list[IPvAnyAddress], Field(min_length=1)]
    rack_position: Reference

    @property
    def configuration_digest(self) -> str:
        configuration = self.model_dump(mode="json", exclude={"ecs_source_ips"})
        if not configuration.get("additional_normal_barcodes"):
            configuration.pop("additional_normal_barcodes", None)
        return canonical_digest(configuration)


class FixedMockRound(_MockRoundBase):
    normal_barcode: Reference
    additional_normal_barcodes: list[Reference] = Field(default_factory=list)
    ng_barcode: Reference
    allocation: Allocated

    @model_validator(mode="after")
    def _different_barcodes(self):
        normal = {self.normal_barcode, *self.additional_normal_barcodes}
        if len(normal) != len(self.additional_normal_barcodes) + 1 or self.ng_barcode in normal:
            raise ValueError("Coarse sorting mock requires distinct normal and NG test barcodes")
        return self

    @property
    def workline_code(self) -> str:
        return self.allocation.workline_code


class RandomMockRound(_MockRoundBase):
    empty_cells: Annotated[list[Allocated], Field(min_length=1)]

    @property
    def workline_code(self) -> str:
        return self.empty_cells[0].workline_code

    @model_validator(mode="after")
    def _distinct_empty_cells(self) -> Self:
        cells = self.empty_cells
        boxes = {(cell.bin_location, cell.bin_code, str(cell.bin_type)) for cell in cells}
        if (
            len({(cell.workline_code, cell.rack_id) for cell in cells}) != 1
            or any(cell.reel_totalthickness != 0 for cell in cells)
            or len({cell.cell_id for cell in cells}) != len(cells)
            or len({(cell.bin_location, cell.cell_index) for cell in cells}) != len(cells)
            or len({box[0] for box in boxes}) != len(boxes)
            or len({box[1] for box in boxes}) != len(boxes)
        ):
            raise ValueError("Mock pool requires distinct empty cells on one workline and rack")
        return self


MockRound = FixedMockRound | RandomMockRound
mock_round_config = TypeAdapter(MockRound)
