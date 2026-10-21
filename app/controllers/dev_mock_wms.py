"""本地联调用的 Mock WMS:`RELIABLE_TARGETS` 的 base_url 指向本应用时,发送器的投递落到这里。

行为模式存进程内存(需单 worker,uvicorn --reload 即是);提交路径无认证,因为发送器不带 token。
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.core import get_current_user
from app.core.clock import unix_ms
from app.core.routing import new_router
from app.integrations.wms_transport.protocol import DEFAULT_SUBMIT_PATH
from app.schemas.dev_outbound import MockWmsMode, MockWmsModeBody

router = new_router(tags=["dev-mock-wms"])

_API_PREFIX = "/api/v1"
_mode = MockWmsMode.accept
_HANG_SECONDS = 60  # 大于任何目标超时即可
_STATUS = {
    MockWmsMode.accept: (202, "RECEIVED"),
    MockWmsMode.duplicate: (200, "DUPLICATE"),
    MockWmsMode.conflict: (409, "CONFLICT"),
    MockWmsMode.reject: (422, "REJECTED"),
    MockWmsMode.unavailable: (503, "UNAVAILABLE"),
}


@router.get("/dev/mock-wms/mode", dependencies=[Depends(get_current_user)])
async def get_mode() -> MockWmsModeBody:
    return MockWmsModeBody(mode=_mode)


@router.put("/dev/mock-wms/mode", dependencies=[Depends(get_current_user)])
async def set_mode(body: MockWmsModeBody) -> MockWmsModeBody:
    """切换 Mock WMS 对出站提交的响应行为,立即对后续投递生效。"""
    global _mode
    _mode = body.mode
    return MockWmsModeBody(mode=_mode)


# DEFAULT_SUBMIT_PATH 含 /api/v1 前缀,而路由挂载时统一加前缀,所以这里去掉。
@router.post(DEFAULT_SUBMIT_PATH.removeprefix(_API_PREFIX), include_in_schema=True)
async def submit_transport_request(request: Request) -> Response:
    """模拟 WMS 的 transport.task.submit@v1 接收端,回显请求里的 operation_id 与任务号。"""
    mode = _mode
    if mode == MockWmsMode.hang:
        await asyncio.sleep(_HANG_SECONDS)
    if mode == MockWmsMode.garbage:
        return PlainTextResponse("not json")
    status_code, code = _STATUS.get(mode, _STATUS[MockWmsMode.accept])
    try:
        sent: Any = json.loads(await request.body())
        data = sent["data"]
        operation_id, task_id = sent["operation_id"], data["transport_task_id"]
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)  # 合同:无法解析的信封返回空 400
    ack_data: dict[str, Any] = {"transport_task_id": task_id}
    if mode == MockWmsMode.reject:
        ack_data["reason_code"] = "INVALID_DATA"
    return JSONResponse(
        {
            "operation_id": operation_id,
            "code": code,
            "timestamp": unix_ms(datetime.now(UTC)),
            "data": ack_data,
        },
        status_code=status_code,
    )
