from app.integrations.wms_callback import callback_operation

from .base import TransportCallback
from .payloads import PositionChanged


@callback_operation("transport.task.member_position_changed@v1")
class MemberPositionChanged(TransportCallback):
    title = "Transport 料箱位置事件"
    Payload = PositionChanged

    def apply(self, state, data, ack):
        state.apply_position(data)
