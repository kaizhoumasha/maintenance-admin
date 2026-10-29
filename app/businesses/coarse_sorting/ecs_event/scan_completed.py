"""Accept original scan facts through the receiver-owned transaction."""

from app.businesses.coarse_sorting.contracts import ScanCompletedData
from app.businesses.runtime import require_runtime
from app.integrations.ecs_callback import EcsEventContext, ecs_event


@ecs_event("SCAN_COMPLETED", device_role="INPUT_ARM", data=ScanCompletedData)
async def scan_completed(ctx: EcsEventContext, payload: ScanCompletedData) -> None:
    await require_runtime(ctx).services["coarse_sorting"].accept_scan(ctx, payload)
