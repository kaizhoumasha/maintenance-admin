"""Single owner declaration for confirmed inbound and outbound arm movements."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.businesses._components.sorting_task.contracts import Face, Identifier, NonNegative
from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .inbound_contracts import Barcode
from .work_plan_contracts import Number


class Data(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ScanPlatform(Data):
    location_id: Literal["STATION_SCAN"]
    location_type: Literal["SCAN_PLATFORM"]


class Reel(Data):
    bin_type: Literal["2", "3", "6"]
    bin_cell_location: int
    reel_layer: int
    reel_thickness: Number
    reel_diameter: Number
    reel_totalthickness: Number


class RackSource(Reel):
    location_id: Literal["STATION_A", "STATION_B"]
    location_type: Literal["ONE_LAYER_RACK"]
    rack_id: Identifier
    bin_id: Identifier
    cell_id: Identifier
    bin_location: str


class BinTarget(Reel):
    location_id: Literal["STATION_WORK1"]
    location_type: Literal["BIN"]


class RackSlot(Data):
    location_id: Literal["STATION_A", "STATION_B"]
    location_type: Literal["FIVE_LAYER_RACK"]
    rack_id: Identifier
    rack_face: Face
    rack_layer: Literal["1", "2", "3", "4", "5"]
    rack_column: str
    reel_thickness: Number
    reel_diameter: Number


class NgTarget(Data):
    location_id: Literal["STATION_NG"]
    location_type: Literal["SCAN_NG"]


class ArmResult(Data):
    actual_qty: int
    pick_and_put_result: Literal["PICK_FAILED", "PUT_FAILED", "PUT_FINISHED", "PICK_EMPTY"]
    barcode: Barcode | None = None
    scanned_at: NonNegative | None = None

    @model_validator(mode="after")
    def quantity(self):
        expected = 1 if self.pick_and_put_result == "PUT_FINISHED" else 0
        if self.actual_qty != expected:
            raise ValueError("quantity contradicts movement stage")
        if self.pick_and_put_result == "PICK_EMPTY" and self.barcode is not None:
            raise ValueError("empty pick has no scan")
        if (self.barcode is None) != (self.scanned_at is None):
            raise ValueError("scan fields must occur together")
        return self


@ecs_command(
    "PICK_AND_PUT",
    device_roles=("BIN_ARM", "RACK_ARM"),
    annex_id="smt_auto.normal_arms",
    annex_version="1",
    result=ArmResult,
    error=ErrorDetail,
    timeout_min_ms=0,
    timeout_max_ms=2**63 - 1,
)
class PickAndPut(Data):
    source: RackSource | BinTarget | RackSlot | ScanPlatform
    target: ScanPlatform | BinTarget | RackSlot | NgTarget

    @model_validator(mode="after")
    def normal_path(self):
        if not (
            (
                isinstance(self.source, (RackSource, BinTarget))
                or isinstance(self.source, RackSlot)
                and self.source.location_id == "STATION_B"
            )
            and isinstance(self.target, ScanPlatform)
            or isinstance(self.source, ScanPlatform)
            and (
                isinstance(self.target, (BinTarget, NgTarget))
                or isinstance(self.target, RackSlot)
                and self.target.location_id == "STATION_A"
            )
        ):
            raise ValueError("only confirmed arm paths are declared")
        return self
