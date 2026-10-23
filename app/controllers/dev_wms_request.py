"""联调控制台:WMS 请求端点(decisions、facts)的 operation 目录、触发入口和状态查询。

operation 由 `app/integrations/<业务>/request` 注册(演示 operation 在 `wms_request/dev.py`),
下拉和目录都来自注册表的 `describe()`,新增 operation 不需要改这里。
"""

import re
from typing import Annotated, Any, Literal, Union

from fastapi import Body, Depends
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.core import SessionDep, error_responses, get_current_user
from app.core.identifiers import uuid7
from app.core.routing import new_router
from app.integrations.wms_request.operation import Registered, operations
from app.schemas.dev_console import DevMessageOut, DevOperationOut, DevRequestOut
from app.services import dev_wms_request_service


class _DevWmsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str | None = None  # 不传由服务端生成 uuid7;重复传同一个可验证幂等重放


def _model(item: Registered) -> type[_DevWmsRequest]:
    name = "DevWmsRequest" + "".join(part.title() for part in re.split(r"[._@]", item.name))
    return create_model(
        name,
        __base__=_DevWmsRequest,
        operation=(Literal[item.name], item.name),
        data=(
            dict[str, Any],
            Field(..., description="Payload,字段见 GET /operations 的 payload_schema"),
        ),
    )


def wms_request_router():
    items = sorted(operations().values(), key=lambda item: item.name)
    # 判别器必须写在 Body(discriminator=...) 上:放进 Annotated 里的 Field 会被 FastAPI 丢弃
    body = Union[tuple(_model(item) for item in items)]  # noqa: UP007  运行时构造
    examples = {
        item.name: {
            "summary": item.operation.title or item.name,
            "value": {"operation": item.name, "data": item.operation.example},
        }
        for item in items
        if item.operation.example is not None
    }
    router = new_router(
        prefix="/dev/wms/requests",
        tags=["dev-wms-request"],
        dependencies=[Depends(get_current_user)],
    )

    @router.get("/operations", response_model=list[DevOperationOut])
    async def list_operations() -> list[dict]:
        """已注册的 WMS 请求 operation:端点、标题、TTL、Payload 的 JSON Schema 和示例。"""
        return [item.describe() for item in sorted(operations().values(), key=lambda i: i.name)]

    @router.post("", response_model=DevRequestOut, responses=error_responses(409))
    async def send_request(
        session: SessionDep,
        req: Annotated[body, Body(discriminator="operation", openapi_examples=examples)],
    ) -> DevRequestOut:
        """在 Examples 下拉里选 operation,走真实的 `WmsRequests.send` 入队(不等于已送达)。"""
        return await dev_wms_request_service.send(
            session,
            req.operation,
            req.operation_id or str(uuid7()),
            req.data,
        )

    @router.get(
        "/status/{operation_id}", response_model=list[DevMessageOut], responses=error_responses(404)
    )
    async def request_status(session: SessionDep, operation_id: str) -> list[DevMessageOut]:
        """按 operation_id 查看请求消息的投递状态、请求与响应原文。"""
        return await dev_wms_request_service.message_status(session, operation_id)

    return router
