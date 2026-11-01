from app.integrations.wms_callback import CallbackErrors, callback_operation

from ..contracts import QueueChanged
from ..service import accept, archive_guard


@callback_operation(
    "sorting.task.queue_changed@v1",
    data=QueueChanged,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    archive_guard=archive_guard,
)
async def queue_changed(ctx, payload):
    return await accept(ctx, payload)
