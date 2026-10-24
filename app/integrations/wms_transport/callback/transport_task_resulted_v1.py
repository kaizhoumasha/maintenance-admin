from app.integrations.wms_callback import CallbackContext, callback_operation

from .base import apply_callback
from .payloads import Resulted


def _apply(state, data, ack):
    if signal := state.register_revision(data):
        raise signal(ack)
    state.apply_result(data)


@callback_operation(
    "transport.task.resulted@v1",
    data=Resulted,
    title="Transport 搬运最终结果",
    echo_keys=("transport_task_id",),
)
async def task_resulted(ctx: CallbackContext, payload: Resulted):
    return await apply_callback(ctx, _apply)
