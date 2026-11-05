from app.integrations.wms_callback import CallbackErrors, Rejected, Unavailable, callback_operation

from ..archive import archive_guard
from ..inbound_contracts import RackExhausted


@callback_operation(
    "putaway.source.rack_exhausted@v1",
    data=RackExhausted,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
    archive_guard=archive_guard,
)
async def exhausted(ctx, payload):
    from app.businesses.runtime import bind_callback_business, require_runtime

    service = require_runtime(ctx).services.get("smt_auto")
    if service is None or getattr(service, "inbound", None) is None:
        raise Unavailable()
    owner = await service.read_task_owner(ctx.session, task_id=payload.task_id)
    if owner is None or owner.business_code != "smt_auto" or owner.task_type != "AUTO":
        raise Unavailable()
    if owner.business_type != "inbound":
        raise Rejected("INVALID_DATA")
    bind_callback_business(ctx, "smt_auto")
    return await service.inbound.accept_source_rack_exhausted(ctx, payload)
