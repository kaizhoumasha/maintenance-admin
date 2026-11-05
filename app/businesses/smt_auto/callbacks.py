"""S1 sorting.task callback handlers; intentionally outside the formal catalog."""

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    Issued,
    PlanDelta,
    QueueChanged,
)
from app.integrations.wms_callback import CallbackErrors, callback_operation

from .task_service import AutoTaskService

_service = AutoTaskService()


@callback_operation("sorting.task.issued@v1", data=Issued, errors=CallbackErrors("INVALID_DATA"))
async def issued(ctx, payload):
    return await _service.accept_task_issued(ctx, payload)


@callback_operation(
    "sorting.task.queue_changed@v1",
    data=QueueChanged,
    errors=CallbackErrors("INVALID_DATA"),
)
async def queue_changed(ctx, payload):
    return await _service.accept_task_queue_changed(ctx, payload)


@callback_operation(
    "sorting.task.plan_delta@v1", data=PlanDelta, errors=CallbackErrors("INVALID_DATA")
)
async def plan_delta(ctx, payload):
    return await _service.accept_task_plan_delta(ctx, payload)


@callback_operation("sorting.task.cancel@v1", data=Cancel, errors=CallbackErrors("INVALID_DATA"))
async def cancel(ctx, payload):
    return await _service.accept_task_cancel(ctx, payload)
