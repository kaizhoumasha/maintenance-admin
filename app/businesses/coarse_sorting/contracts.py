"""Frozen scan-acceptance payload; business validation and commands are separate stages."""

from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, field_validator

from app.integrations.ecs_callback import CallbackPayload


class ScanCompletedData(CallbackPayload):
    barcode: str
    box_barcodes: str | None = None
    scan_time: Annotated[int, Field(ge=0, le=2**63 - 1)]
    reel_diameter: int | FiniteFloat
    reel_thickness: int | FiniteFloat
    result: Literal["OK", "NG"]

    @field_validator("box_barcodes")
    @classmethod
    def supplied_box_barcodes_must_be_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("box_barcodes must be a string when supplied")
        return value
