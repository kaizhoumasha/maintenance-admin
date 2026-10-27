"""Authenticated local command debugging; the loopback mock only acknowledges receipt."""

from dataclasses import asdict, dataclass
from ipaddress import ip_address
from typing import Annotated, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core import SessionDep, error_responses, get_current_user, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.ratelimit import RateLimit
from app.core.routing import new_router
from app.integrations.ecs_command.contracts import CommandEnvelope, CommandView, Text
from app.integrations.ecs_command.dev import (
    DevEcsRoutes,
    create_debug_command,
    get_debug_command,
)
from app.integrations.ecs_command.runtime import TARGET
from app.integrations.ecs_command.service import CommandOwner
from app.models import User
from app.services.device_routes import DeviceCommandRoutes


class DevCommandIn(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "client_request_id": "demo-1",
                    "ecs_url": "http://127.0.0.1:8000",
                    "device_code": "D1",
                    "task_type": "MOVE",
                    "timeout_ms": 30000,
                    "params": {"qty": 1},
                }
            ]
        },
    )
    client_request_id: Text
    ecs_url: Text
    device_code: Text
    task_type: Text
    timeout_ms: Annotated[int, Field(ge=0, le=2**63 - 1)]
    params: dict


@dataclass(frozen=True)
class DevCommandView(CommandView):
    ecs_url: str


@dataclass(frozen=True)
class DevCommandOut(DevCommandView):
    client_request_id: str


class MockParams(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    qty: int


class MockCommand(CommandEnvelope):
    device_code: Literal["D1"]
    task_type: Literal["MOVE"]
    timeout: Annotated[int, Field(ge=1, le=60000)]
    params: MockParams


router = new_router(prefix="/dev/ecs/commands", tags=["dev-ecs-command"])
mock_router = new_router(tags=["dev-ecs-mock"], dependencies=[Depends(RateLimit(None))])
CurrentUser = Annotated[User, Depends(get_current_user)]
check_send_permission = require_permission("dev:ecs:send_real")
COMMAND_EXAMPLE = asdict(
    CommandView(
        "01900000-0000-7000-8000-000000000001",
        "QUEUED",
        "PENDING",
        False,
        None,
        None,
        False,
    )
)


def _service(request):
    service = getattr(request.app.state, "ecs_commands", None)
    if (
        service is None
        or service.outbox is None
        or service.target != TARGET
        or not isinstance(service.routes, (DevEcsRoutes, DeviceCommandRoutes))
        or getattr(request.app.state, "outbox", None) is not service.outbox
    ):
        raise AppError(
            ErrorCode.DATABASE_UNAVAILABLE,
            "ECS runtime is not ready; start the application lifespan with ECS assembly",
        )
    return service


@router.post(
    "",
    response_model=DevCommandOut,
    responses={
        **error_responses(403, 409, 503),
        200: {
            "content": {
                "application/json": {
                    "example": {
                        **COMMAND_EXAMPLE,
                        "ecs_url": "http://127.0.0.1:8000",
                        "client_request_id": "demo-1",
                    }
                }
            }
        },
    },
)
async def create_command(
    request: Request, session: SessionDep, user: CurrentUser, body: DevCommandIn
):
    """登记调试命令并提交事务；返回命令身份，不代表已发送或完成。"""
    service = _service(request)

    async def authorize_send():
        await check_send_permission(request, session, user)

    command, origin = await create_debug_command(
        session,
        service=service,
        owner=CommandOwner(f"dev.ecs:{user.id}"),
        client_request_id=body.client_request_id,
        ecs_url=body.ecs_url,
        device_code=body.device_code,
        task_type=body.task_type,
        timeout_ms=body.timeout_ms,
        params=body.params,
        authorize_send=authorize_send,
    )
    await session.commit()
    return DevCommandOut(
        **asdict(command), ecs_url=origin, client_request_id=body.client_request_id
    )


@router.get(
    "/{command_code}",
    response_model=DevCommandView,
    responses={
        **error_responses(404, 503),
        200: {
            "content": {
                "application/json": {
                    "example": {**COMMAND_EXAMPLE, "ecs_url": "http://127.0.0.1:8000"}
                }
            }
        },
    },
)
async def get_command(request: Request, session: SessionDep, user: CurrentUser, command_code: str):
    """只返回当前登录用户的公开命令视图；SUCCESS不等于业务已消费。"""
    command = await get_debug_command(
        session,
        service=_service(request),
        owner=CommandOwner(f"dev.ecs:{user.id}"),
        command_code=command_code,
    )
    if command is None:
        raise AppError(ErrorCode.NOT_FOUND, "ECS command not found")
    view, origin = command
    return DevCommandView(**asdict(view), ecs_url=origin)


@mock_router.post("/device/command")
async def acknowledge_command(request: Request, body: MockCommand):
    """仅回环调用可用；严格校验演示合同后只ACK，不产生Result或业务消费。"""
    try:
        loopback = request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise AppError(ErrorCode.FORBIDDEN, "ECS mock accepts loopback callers only")
    return {"code": 200, "message": "ACK"}
