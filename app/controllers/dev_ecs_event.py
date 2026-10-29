"""Authenticated local event debugging, with examples from the existing event registry."""

from typing import Annotated

from fastapi import BackgroundTasks, Depends, Path, Request

from app.controllers.ecs_callback import receive_event_request
from app.core import error_responses, get_current_user
from app.core.database import session_factory
from app.core.routing import new_router
from app.integrations.ecs_callback.envelope import EcsEventEnvelope
from app.integrations.ecs_callback.registry import events
from app.schemas.dev_ecs_event import DevEcsEventDelivery, DevEcsEventState
from app.services.dev_ecs_event_service import BusinessReader, event_state


def ecs_event_router(*, read_business: BusinessReader | None = None):
    examples = {}
    for (role, name), item in events().items():
        schema = item.data_model.model_json_schema() if item.data_model is not None else {}
        samples = schema.get("examples") or []
        examples[f"{role}:{name}"] = {
            "summary": f"{role} / {name}",
            "description": "设备码和时间戳为样例，请按测试绑定修改。"
            + ("" if samples else "该 Payload 无样例，请填写 data，字段见注册目录。"),
            "value": {
                "device_code": f"{role}-01",
                "event_type": name,
                "timestamp": 1793206799000,
                "data": samples[0] if samples else {},
            },
        }
    router = new_router(
        prefix="/dev/ecs/events",
        tags=["dev-ecs-event"],
        dependencies=[Depends(get_current_user)],
    )

    @router.post(
        "",
        response_model=DevEcsEventDelivery,
        response_description=(
            "一次接收尝试的观察结果；接收器状态见 status_code，不代表业务受理成功。"
        ),
        responses=error_responses(400),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": EcsEventEnvelope.model_json_schema(),
                        "examples": examples,
                    }
                },
            }
        },
    )
    async def deliver_event(
        request: Request, background_tasks: BackgroundTasks
    ) -> DevEcsEventDelivery:
        """选择 Examples，修改绑定设备码与时间戳；原样重报可验证幂等。"""
        result = await receive_event_request(request, background_tasks)
        return DevEcsEventDelivery(
            digest=result.digest,
            status_code=result.status_code,
            outcome=result.outcome,
            response=result.body,
            diagnostic=result.diagnostic,
        )

    @router.get("/{digest}", response_model=DevEcsEventState, responses=error_responses(404))
    async def query_event(
        request: Request, digest: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
    ):
        """查看持久收据与装配提供的业务投影；不推断物理完成或自动重放。"""
        factory = getattr(request.app.state, "callback_session_factory", session_factory)
        return await event_state(factory, digest, read_business)

    return router
