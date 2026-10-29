"""The original conveyor move and its versioned completion evidence."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail


class MoveForwardResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    # FAILED may have empty data; the business consumer requires both fields for SUCCESS.
    actual_qty: Annotated[int, Field(ge=1, le=1)] | None = None
    location: Literal["STATION_PIPELINE1_OUTPUT1"] | None = None


@ecs_command(
    "MOVE_FORWARD",
    device_roles=("CONVEYOR",),
    annex_id="coarse_sorting.move_forward",
    annex_version="1",
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
