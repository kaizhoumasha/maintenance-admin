"""仅 local 的事件示例;真实事件文件名用 registration_filename(event_type)。"""

from pydantic import ConfigDict, Field

from . import CallbackPayload
from .binding import BindingResolver, DeviceBinding
from .registry import EcsEventContext, ecs_event

EXAMPLE_SCAN_ROLE = "EXAMPLE_SCAN"


class ExampleScan(CallbackPayload):
    barcode: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(json_schema_extra={"examples": [{"barcode": "BIN-1"}]})


@ecs_event("EXAMPLE_SCAN", device_role=EXAMPLE_SCAN_ROLE, data=ExampleScan)
async def example_scan(ctx: EcsEventContext, payload: ExampleScan) -> None:
    ctx.note("LOCAL_EXAMPLE", barcode=payload.barcode)


def local_bindings(fallback: BindingResolver | None) -> BindingResolver:
    async def resolve(session, device_code):
        if device_code == "LOCAL_EXAMPLE_SCAN":
            return DeviceBinding(
                device_id=0,
                device_code=device_code,
                device_version=1,
                work_line_id=0,
                work_line_version=1,
                device_role=EXAMPLE_SCAN_ROLE,
                device_bindings={EXAMPLE_SCAN_ROLE: device_code},
                position_bindings={},
            )
        return await fallback(session, device_code) if fallback else None

    return resolve
