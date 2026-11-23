from app.integrations.ecs_callback import EcsEventContext, ecs_event

from ..definition import XRAY
from ..event_data import ArrivalData


@ecs_event("MATERIAL_ARRIVED", device_role=XRAY, data=ArrivalData)
async def material_arrived(ctx: EcsEventContext, payload: ArrivalData) -> None:
    from app.businesses.runtime import require_runtime

    await require_runtime(ctx).services["xray"].accept(ctx, "OUTLET", payload)
