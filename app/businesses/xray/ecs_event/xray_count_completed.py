from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..definition import XRAY
from ..event_data import CountData


@ecs_event("XRAY_COUNT_COMPLETED", device_role=XRAY, data=CountData)
async def xray_count_completed(ctx: EcsEventContext, payload: CountData) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["xray"].accept(ctx, "COUNT", payload)
