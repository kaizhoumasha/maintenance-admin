from app.integrations.wms_callback import CallbackErrors, callback_operation

from ..contracts import Cancel
from ..service import accept, archive_guard


@callback_operation(
    "sorting.task.cancel@v1",
    data=Cancel,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    archive_guard=archive_guard,
)
async def cancel(ctx, payload):
    return await accept(ctx, payload)
