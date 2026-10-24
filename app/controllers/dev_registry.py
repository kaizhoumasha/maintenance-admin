"""本进程实际加载的三类注册项,仅由 local dev 控制台挂载。"""

from dataclasses import asdict

from fastapi import Depends

from app.core import get_current_user
from app.core.routing import new_router
from app.integrations.ecs_callback.registry import events
from app.integrations.wms_callback.registry import operations as callbacks
from app.integrations.wms_request.operation import operations as requests
from app.schemas.dev_console import DevRegistrationOut

router = new_router(
    prefix="/dev/integrations",
    tags=["dev-integrations"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/registry", response_model=list[DevRegistrationOut])
async def list_registrations() -> list[dict]:
    """查看实际注册名称、来源、local-only 及 Payload;缺项时先检查入口目录和启动日志。"""
    rows = []
    for kind, items in (
        ("ecs_event", events()),
        ("wms_callback", callbacks()),
        ("wms_request", requests()),
    ):
        for name, item in items.items():
            model = item.data_model
            endpoint = getattr(item, "endpoint", None)
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    **asdict(item.registration_source),
                    "endpoint": endpoint.value if endpoint is not None else None,
                    "payload_schema": model.model_json_schema() if model is not None else None,
                }
            )
    return sorted(rows, key=lambda row: (row["kind"], row["name"]))
