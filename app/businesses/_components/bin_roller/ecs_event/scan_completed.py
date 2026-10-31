"""One thin role-bound entry for all four physical roller points."""

from app.businesses.runtime import require_component
from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..contracts import ScanCompletedData


@ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=ScanCompletedData)
@ecs_event("SCAN_COMPLETED", device_role="SCAN2", data=ScanCompletedData)
@ecs_event("SCAN_COMPLETED", device_role="SCAN3", data=ScanCompletedData)
@ecs_event("SCAN_COMPLETED", device_role="SCAN4", data=ScanCompletedData)
async def scan_completed(ctx: EcsEventContext, payload: ScanCompletedData) -> None:
    await require_component(ctx, "bin_roller").accept_scan(ctx, payload)
