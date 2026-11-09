from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..definition import XRAY
from ..event_data import ScanData


@ecs_event("SCAN_COMPLETED", device_role=XRAY, data=ScanData)
async def scan_completed(ctx: EcsEventContext, payload: ScanData) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["xray"].accept(ctx, payload.scanStage, payload)
