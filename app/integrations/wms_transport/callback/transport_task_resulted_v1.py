from app.integrations.wms_callback import callback_operation

from .base import TransportCallback
from .payloads import Resulted


@callback_operation("transport.task.resulted@v1")
class TaskResulted(TransportCallback):
    title = "Transport 搬运最终结果"
    Payload = Resulted

    def apply(self, state, data, ack):
        if signal := state.register_revision(data):
            raise signal(ack)
        state.apply_result(data)
