from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..definition import XRAY
from ..event_data import NgDischargedData


@ecs_event("NG_DISCHARGED", device_role=XRAY, data=NgDischargedData)
async def ng_discharged(ctx: EcsEventContext, payload: NgDischargedData) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["xray"].accept(ctx, "NG", payload)
