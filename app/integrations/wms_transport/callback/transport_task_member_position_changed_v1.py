from app.integrations.wms_callback import CallbackContext, callback_operation

from .base import apply_callback
from .payloads import PositionChanged


def _apply(state, data, ack):
    state.apply_position(data)


@callback_operation(
    "transport.task.member_position_changed@v1",
    data=PositionChanged,
    title="Transport 料箱位置事件",
    echo_keys=("transport_task_id",),
)
async def member_position_changed(ctx: CallbackContext, payload: PositionChanged):
    return await apply_callback(ctx, _apply)
