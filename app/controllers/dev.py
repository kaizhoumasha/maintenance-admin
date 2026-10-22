"""联调控制台:仅 ENVIRONMENT=local 挂载。统一入口按场景下拉触发出站,另有按 business_key 的状态节点。

场景由 app/integrations/<业务>/dev.py 里的 @dev_scenario 登记,启动时自动扫描,新增业务无需改这里。
"""

import importlib
import pkgutil
from typing import Annotated, Union

from fastapi import Body, Depends, FastAPI

from app import integrations
from app.controllers.dev_mock_wms import router as mock_wms_router
from app.core import SessionDep, error_responses, get_current_user, get_settings, logger
from app.core.dev_scenarios import DevScenario, scenarios
from app.core.routing import new_router
from app.reliable.config import parse_reliable_config
from app.schemas.dev_outbound import DevMessageOut, DevSubmitOut
from app.services import dev_outbound_service


def _load_dev_modules() -> None:
    for info in sorted(pkgutil.iter_modules(integrations.__path__), key=lambda i: i.name):
        if not info.ispkg:
            continue
        name = f"{integrations.__name__}.{info.name}.dev"
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:  # 模块自身的导入错误必须暴露,不能让场景静默缺失
                raise


def _outbound_router(items: list[DevScenario]):
    by_name = {s.name: s for s in items}
    # 判别器必须写在 Body(discriminator=...) 上:放进 Annotated 里的 Field 会被 FastAPI 丢弃
    body = Union[tuple(s.model for s in items)]  # noqa: UP007  运行时构造
    examples = {
        s.name: {"summary": s.title, "value": {"scenario": s.name, **s.example}} for s in items
    }
    router = new_router(
        prefix="/dev/outbound", tags=["dev-outbound"], dependencies=[Depends(get_current_user)]
    )

    @router.post("", response_model=DevSubmitOut, responses=error_responses(409))
    async def submit_outbound(
        session: SessionDep,
        req: Annotated[body, Body(discriminator="scenario", openapi_examples=examples)],
    ) -> DevSubmitOut:
        """在 Examples 下拉里选场景,走真实的业务接入层登记出站义务(不等于已送达)。"""
        scenario = by_name[req.scenario]
        outcome = await dev_outbound_service.submit(session, scenario, req)
        return DevSubmitOut(
            scenario=scenario.name,
            title=scenario.title,
            business_key=outcome.business_key,
            client_request_id=outcome.client_request_id,
        )

    @router.get(
        "/status/{business_key}", response_model=list[DevMessageOut], responses=error_responses(404)
    )
    async def outbound_status(session: SessionDep, business_key: str) -> list[DevMessageOut]:
        """按 business_key 查看出站消息的投递状态、请求与响应原文。"""
        return await dev_outbound_service.message_status(session, business_key)

    return router


def include_dev_routers(app: FastAPI, *, environment: str, prefix: str = "/api/v1") -> None:
    if environment != "local":
        return
    _load_dev_modules()
    items = scenarios()
    if items:
        app.include_router(_outbound_router(items), prefix=prefix)
    # Mock WMS 是 Transport 协议专用的,显式挂载,不走场景自动扫描;以后别的协议要 mock 再加一行
    app.include_router(mock_wms_router, prefix=prefix)
    if not parse_reliable_config(get_settings()).targets:
        logger.warning(
            "dev console mounted but RELIABLE_TARGETS has no target: submitting fails with 500. "
            'Example (mock WMS on this app): RELIABLE_TARGETS=\'{"targets":{"wms-transport":'
            '{"base_url":"http://127.0.0.1:8000","protocol":"wms-transport"}}}\''
        )
