from app.integrations.wms_callback import CallbackErrors, callback_operation

from ..contracts import Issued
from ..service import accept, archive_guard


@callback_operation(
    "sorting.task.issued@v1",
    data=Issued,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    archive_guard=archive_guard,
)
async def issued(ctx, payload):
    return await accept(ctx, payload)
