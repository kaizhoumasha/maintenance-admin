"""Confirmed normal inbound fields; exception decisions retain their original pending state."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.businesses._components.sorting_task.contracts import Identifier, NonNegative

from .work_plan_contracts import Data, Number

Barcode = Annotated[str, Field(min_length=1, max_length=1000)]


class Source(Data):
    rack_id: Identifier
    bin_code: Identifier
    cell_id: Identifier


class Target(Data):
    bin_code: Identifier
    cell_id: Identifier
    bin_type: Literal[2, 3, 6]
    cell_index: Annotated[int, Field(ge=1, le=7)]
    reel_layer: int
    reel_thickness: Number
    reel_diameter: Number
    reel_totalthickness: Number


class Scan(Data):
    actual_qty: Literal[1]
    pick_and_put_result: Literal["PUT_FINISHED"]
    barcode: Barcode
    scanned_at: NonNegative


class NgLocator(Data):
    type: Literal["NG_ZONE"]
    zone_code: Literal["STATION_NG"]


class Reject(Data):
    result: Literal["REJECT"]
    business_exception_code: Literal["MATERIAL_REJECTED", "SOURCE_CELL_MISMATCH"]
    ng_locator: NgLocator
    source_disposition: Literal["CONTINUE", "CLOSE"]

    @model_validator(mode="after")
    def disposition(self):
        if (
            self.business_exception_code == "SOURCE_CELL_MISMATCH"
            and self.source_disposition != "CLOSE"
        ):
            raise ValueError("mismatched source must close")
        return self


class Retry(Data):
    result: Literal["RETRY"]


class SourceAbsent(Data):
    result: Literal["SOURCE_ABSENT"]
    source_rack_exhausted: Literal[True] | None = None

    @model_validator(mode="before")
    @classmethod
    def exhaustion(cls, value):
        if (
            isinstance(value, dict)
            and "source_rack_exhausted" in value
            and value["source_rack_exhausted"] is not True
        ):
            raise ValueError("exhaustion must be explicit true or absent")
        return value


class RackExhausted(Data):
    task_id: Identifier
    rack_id: Identifier
    transport_task_id: Identifier
