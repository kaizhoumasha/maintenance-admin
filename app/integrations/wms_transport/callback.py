"""Transport 的两个 WMS 回调 operation(合同 §5):位置事件与最终结果。

自动发现:本文件由 `wms_callback.discovery` 在启动时导入,装饰器完成注册,不改控制器和 `main.py`。
两个 operation 都在请求事务内锁住 `transport_task` 行后直接应用,所有响应回显 `transport_task_id`。
规则见 `callback_apply.py`,设计见 docs/superpowers/specs/2026-10-22-transport-callback-design.md。
"""

from sqlalchemy import select

from app.integrations.wms_callback import (
    Accepted,
    CallbackContext,
    CallbackOperation,
    callback_operation,
)
from app.models.transport import TransportTask

from .callback_apply import TaskCallbacks
from .callback_payloads import PositionChanged, Resulted
from .settle import settle_submitted


class _TransportCallback(CallbackOperation):
    echo_keys = ("transport_task_id",)

    async def _locked_task(self, ctx: CallbackContext, task_id: str) -> TransportTask | None:
        return await ctx.session.scalar(
            select(TransportTask)
            .where(TransportTask.transport_task_id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def execute(self, ctx: CallbackContext, payload):
        data = ctx.parsed  # 已是 Payload 规范化后的 dict,不必再转一次
        ack = {"transport_task_id": data["transport_task_id"]}
        task = await self._locked_task(ctx, data["transport_task_id"])
        if task is None:
            ctx.note("UNKNOWN_TASK", transport_task_id=ack["transport_task_id"], data=data)
            return ack
        await settle_submitted(ctx.session, task.transport_task_id)  # 对端已知该任务,不必再重试提交
        if task.frozen is None:
            ctx.note("FROZEN_MISSING", transport_task_id=ack["transport_task_id"], data=data)
            return ack
        state = TaskCallbacks(task, ctx)
        self.apply(state, data, ack)  # 判重为 Duplicate/Conflict 时直接抛出
        return Accepted(ack)

    def apply(self, state: TaskCallbacks, data: dict, ack: dict) -> None:
        raise NotImplementedError


@callback_operation("transport.task.member_position_changed@v1")
class MemberPositionChanged(_TransportCallback):
    title = "Transport 料箱位置事件"
    Payload = PositionChanged

    def apply(self, state, data, ack):
        state.apply_position(data)


@callback_operation("transport.task.resulted@v1")
class TaskResulted(_TransportCallback):
    title = "Transport 搬运最终结果"
    Payload = Resulted

    def apply(self, state, data, ack):
        if signal := state.register_revision(data):
            raise signal(ack)
        state.apply_result(data)
