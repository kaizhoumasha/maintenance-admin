"""联调控制台的回调投递:对已提交的 Transport 任务投递样例回调,并查看任务当前状态。

仅 ENVIRONMENT=local 挂载(随 `include_dev_routers`)。样例回调走真实接收管线,等同 WMS 回调。
"""

from fastapi import Depends, Request

from app.core import error_responses, get_current_user
from app.core.database import session_factory
from app.core.routing import new_router
from app.schemas.dev_callback import DevCallbackBody, DevCallbackOut, DevTaskStateOut
from app.services import dev_callback_service

router = new_router(
    prefix="/dev/transport",
    tags=["dev-transport"],
    dependencies=[Depends(get_current_user)],
)


def _factory(request: Request):
    return getattr(request.app.state, "callback_session_factory", session_factory)


@router.post(
    "/{transport_task_id}/callbacks",
    response_model=list[DevCallbackOut],
    responses=error_responses(404, 400),
)
async def deliver_callbacks(
    request: Request, transport_task_id: str, body: DevCallbackBody
) -> list[DevCallbackOut]:
    """按冻结事实构造样例回调并逐条投递;先 `position_unknown` 再 `succeeded` 可看收敛。"""
    return await dev_callback_service.deliver(_factory(request), transport_task_id, body.scenario)


@router.get("/{transport_task_id}", response_model=DevTaskStateOut, responses=error_responses(404))
async def transport_task_state(request: Request, transport_task_id: str) -> DevTaskStateOut:
    """查看任务行上回调应用后的状态:终态、已应用结果、料箱位置、暂存结果。"""
    return await dev_callback_service.task_state(_factory(request), transport_task_id)
