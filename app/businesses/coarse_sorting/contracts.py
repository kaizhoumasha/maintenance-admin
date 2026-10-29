"""Frozen scan-acceptance payload; business validation and commands are separate stages."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, FiniteFloat

from app.integrations.ecs_callback import CallbackPayload


class ScanCompletedData(CallbackPayload):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "barcode": "TEST-REEL-001",
                    "scan_time": 1793206798000,
                    "reel_diameter": 15,
                    "reel_thickness": 20,
                    "result": "OK",
                }
            ]
        }
    )

    barcode: str
    scan_time: Annotated[int, Field(ge=0, le=2**63 - 1)]
    reel_diameter: int | FiniteFloat
    reel_thickness: int | FiniteFloat
    result: Literal["OK", "NG"]
