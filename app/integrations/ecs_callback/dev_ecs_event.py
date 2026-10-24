"""仅 local 的事件示例;真实事件文件名用 registration_filename(event_type)。"""

from pydantic import ConfigDict, Field

from . import CallbackPayload
from .registry import EcsEventContext, ecs_event


class ExampleScan(CallbackPayload):
    barcode: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(json_schema_extra={"examples": [{"barcode": "BIN-1"}]})


@ecs_event("EXAMPLE_SCAN", data=ExampleScan)
async def example_scan(ctx: EcsEventContext, payload: ExampleScan) -> None:
    ctx.note("LOCAL_EXAMPLE", barcode=payload.barcode)
