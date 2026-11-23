from app.integrations.wms_callback import CallbackErrors, callback_operation

from ..contracts import PlanDelta
from ..service import accept, archive_guard, recheck


@callback_operation(
    "sorting.task.plan_delta@v1",
    data=PlanDelta,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    archive_guard=archive_guard,
    recheck=recheck,
)
async def plan_delta(ctx, payload):
    return await accept(ctx, payload)
