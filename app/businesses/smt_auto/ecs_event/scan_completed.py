"""Accept scanner evidence for the original outbound movement to the scan platform."""

from typing import Annotated, Literal

from pydantic import Field

from app.businesses._components.sorting_task.contracts import Identifier
from app.integrations.ecs_callback import CallbackPayload, EcsEventContext, ecs_event

from ..inbound_contracts import Barcode


class PlatformScan(CallbackPayload):
    location: Literal["SCAN_PLATFORM"]
    barcode: Annotated[Barcode, Field(pattern=r"^[^\x00]+$")]
    previous_command_code: Identifier
    scan_revision: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)] = 1


@ecs_event("SCAN_COMPLETED", device_role="PLATFORM_SCANNER", data=PlatformScan)
async def scan_completed(ctx: EcsEventContext, payload: PlatformScan) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["smt_auto"].outbound.accept_platform_scan(ctx, payload)
