"""本进程的三类业务注册项及内置协议能力，仅由 local dev 控制台挂载。"""

from dataclasses import asdict

from fastapi import Depends, Request

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
async def list_registrations(request: Request) -> list[dict]:
    """查看实际注册名称、来源、local-only 及 Payload;缺项时先检查入口目录和启动日志。"""
    rows = []
    for kind, items in (
        ("ecs_event", events()),
        ("wms_callback", callbacks()),
        ("wms_request", requests()),
    ):
        for key, item in items.items():
            role, name = key if kind == "ecs_event" else (None, key)
            model = item.data_model
            endpoint = getattr(item, "endpoint", None)
            rows.append(
                {
                    "kind": kind,
                    "entry_type": "business_registration",
                    "name": name,
                    "device_role": role,
                    **asdict(item.registration_source),
                    "endpoint": endpoint.value if endpoint is not None else None,
                    "payload_schema": model.model_json_schema() if model is not None else None,
                }
            )
    rows.extend(getattr(request.app.state, "integration_capabilities", ()))
    service = getattr(request.app.state, "ecs_commands", None)
    routes = getattr(service, "routes", None)
    for entry in getattr(routes, "bindings", {}).values():
        models = service.annexes[(entry.annex_id, entry.annex_version)]
        rows.append(
            {
                "kind": "ecs_command",
                "entry_type": "command_contract",
                "name": entry.task_type,
                "device_role": entry.device_role,
                **asdict(entry.registration_source),
                "endpoint": None,
                "payload_schema": models.params_model.model_json_schema(),
                "response_schema": models.result_model.model_json_schema(),
            }
        )
    return sorted(rows, key=lambda row: (row["kind"], row.get("device_role") or "", row["name"]))
