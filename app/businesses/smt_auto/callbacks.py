"""Unregistered S1 delegates; shared sorting_task owns operation registration."""

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    Issued,
    PlanDelta,
    QueueChanged,
)

from .task_service import AutoTaskService

_service = AutoTaskService()


async def issued(ctx, payload: Issued):
    return await _service.accept_task_issued(ctx, payload)


async def queue_changed(ctx, payload: QueueChanged):
    return await _service.accept_task_queue_changed(ctx, payload)


async def plan_delta(ctx, payload: PlanDelta):
    return await _service.accept_task_plan_delta(ctx, payload)


async def cancel(ctx, payload: Cancel):
    return await _service.accept_task_cancel(ctx, payload)
