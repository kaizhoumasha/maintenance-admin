from app.integrations.wms_callback import CallbackErrors, Rejected, Unavailable, callback_operation

from ..contracts import ManualCompleted


@callback_operation(
    "manual_bin.work_completed@v1",
    data=ManualCompleted,
    errors=CallbackErrors("INVALID_DATA", "INVALID_ENVELOPE", "IDEMPOTENCY_CONFLICT"),
)
async def completed(ctx, payload):
    from app.businesses.runtime import bind_callback_business, require_runtime

    if payload.completed_at > ctx.timestamp:
        raise Rejected("INVALID_DATA")
    service = require_runtime(ctx).services.get("smt_manual")
    if service is None:
        raise Unavailable()
    owner = await service.read_task_owner(ctx.session, task_id=payload.task_id)
    if owner is None or owner.business_code != "smt_manual" or owner.task_type != "MANUAL":
        raise Unavailable()
    try:
        bind_callback_business(ctx, "smt_manual")
    except Rejected:
        raise Unavailable() from None
    return await service.accept_manual_completed(ctx, payload)
