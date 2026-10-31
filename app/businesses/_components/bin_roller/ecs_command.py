"""Verified direction commands retain the supplier's empty params and Result envelope."""

from pydantic import BaseModel, ConfigDict

from app.integrations.ecs_command import ecs_command
from app.integrations.ecs_command.contracts import ErrorDetail

from .contracts import DirectionResult


@ecs_command(
    "MOVE_FORWARD",
    device_roles=("SCAN1", "SCAN2", "SCAN3", "SCAN4"),
    annex_id="bin_roller.move_forward",
    annex_version="1",
    result=DirectionResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class MoveForward(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


@ecs_command(
    "MOVE_RIGHT",
    device_roles=("SCAN1",),
    annex_id="bin_roller.move_right",
    annex_version="1",
    result=DirectionResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class MoveRight(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


@ecs_command(
    "MOVE_LEFT",
    device_roles=("SCAN3",),
    annex_id="bin_roller.move_left",
    annex_version="1",
    result=DirectionResult,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=60000,
)
class MoveLeft(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
