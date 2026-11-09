from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..definition import XRAY
from ..event_data import NgRequestedData


@ecs_event("XRAY_NG_REQUESTED", device_role=XRAY, data=NgRequestedData)
async def xray_ng_requested(ctx: EcsEventContext, payload: NgRequestedData) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["xray"].accept(ctx, "NG_REQUEST", payload)
