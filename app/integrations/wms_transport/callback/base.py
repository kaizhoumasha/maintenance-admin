from collections.abc import Callable

from sqlalchemy import select

from app.integrations.wms_callback import Accepted, CallbackContext
from app.models.transport import TransportTask

from ..settle import settle_submitted
from .apply import TaskCallbacks


async def apply_callback(
    ctx: CallbackContext, apply: Callable[[TaskCallbacks, dict, dict], None]
) -> Accepted | dict:
    data = ctx.parsed  # 保留 Payload 规范化后的 dict
    ack = {"transport_task_id": data["transport_task_id"]}
    task = await ctx.session.scalar(
        select(TransportTask)
        .where(TransportTask.transport_task_id == data["transport_task_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        ctx.note("UNKNOWN_TASK", transport_task_id=ack["transport_task_id"], data=data)
        return ack
    await settle_submitted(ctx.session, task.transport_task_id)  # 对端已知该任务,不必再重试提交
    if task.frozen is None:
        ctx.note("FROZEN_MISSING", transport_task_id=ack["transport_task_id"], data=data)
        return ack
    apply(TaskCallbacks(task, ctx), data, ack)  # 判重为 Duplicate/Conflict 时直接抛出
    return Accepted(ack)
