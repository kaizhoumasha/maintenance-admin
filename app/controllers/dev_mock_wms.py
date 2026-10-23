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
from app.integrations.wms_request import ADAPTERS as REQUEST_ADAPTERS
from app.integrations.wms_request import DECISIONS_PROTOCOL, FACTS_PROTOCOL
from app.integrations.wms_transport.protocol import DEFAULT_SUBMIT_PATH
from app.schemas.dev_console import MockWmsMode, MockWmsModeBody

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
    MockWmsMode.busy: (429, "BUSY"),
}
_RETRY_AFTER_MS = 1000


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
@router.post(DEFAULT_SUBMIT_PATH.removeprefix(_API_PREFIX))
async def submit_transport_request(request: Request) -> Response:
    """模拟 WMS 的 transport.task.submit@v1 接收端,回显请求里的 operation_id 与任务号。"""
    mode = _mode
    if mode == MockWmsMode.hang:
        await asyncio.sleep(
            _HANG_SECONDS
        )  # 通常发送方超时先断开;睡完仍无意义响应,与 garbage 同判 UNKNOWN
    if mode in (MockWmsMode.garbage, MockWmsMode.hang):
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
    if mode == MockWmsMode.busy:
        ack_data["retry_after_ms"] = _RETRY_AFTER_MS
    return JSONResponse(
        {
            "operation_id": operation_id,
            "code": code,
            "timestamp": unix_ms(datetime.now(UTC)),
            "data": ack_data,
        },
        status_code=status_code,
    )


async def _answer_request(
    request: Request, accept: tuple[int, str], duplicate: tuple[int, str]
) -> Response:
    """facts、decisions 共用:不认识具体 operation,只回显 operation_id,`data` 恒为空对象。"""
    mode = _mode
    if mode == MockWmsMode.hang:
        await asyncio.sleep(_HANG_SECONDS)
    if mode in (MockWmsMode.garbage, MockWmsMode.hang):
        return PlainTextResponse("not json")
    try:
        sent: Any = json.loads(await request.body())
        operation_id = sent["operation_id"]
        if not isinstance(sent["data"], dict):
            raise TypeError("data must be an object")
    except (ValueError, KeyError, TypeError):
        return Response(status_code=400)  # 合同:无法解析的信封返回空 400
    success = {
        MockWmsMode.accept: accept,
        MockWmsMode.duplicate: duplicate,
        MockWmsMode.prepare_accepted: (202, "PREPARE_ACCEPTED")
        if accept[1] == "DECIDED"
        else accept,
    }
    failure = {
        MockWmsMode.conflict: (409, "CONFLICT", {"reason_code": "IDEMPOTENCY_CONFLICT"}),
        MockWmsMode.reject: (422, "REJECTED", {"reason_code": "INVALID_DATA"}),
        MockWmsMode.unavailable: (503, "UNAVAILABLE", {}),
        MockWmsMode.busy: (429, "BUSY", {"retry_after_ms": _RETRY_AFTER_MS}),
    }
    if mode in success:
        (status_code, code), data = success[mode], {}
    else:
        status_code, code, data = failure[mode]
    return JSONResponse(
        dict(operation_id=operation_id, code=code, timestamp=unix_ms(datetime.now(UTC)), data=data),
        status_code=status_code,
    )


@router.post(REQUEST_ADAPTERS[FACTS_PROTOCOL].path.removeprefix(_API_PREFIX))
async def record_fact(request: Request) -> Response:
    """模拟 WMS 的 facts 接收端。"""
    return await _answer_request(request, (200, "RECORDED"), (200, "DUPLICATE"))


@router.post(REQUEST_ADAPTERS[DECISIONS_PROTOCOL].path.removeprefix(_API_PREFIX))
async def decide_request(request: Request) -> Response:
    """模拟 WMS 的 decisions 接收端;同身份重放返回首次决定,所以 duplicate 仍是 DECIDED。"""
    return await _answer_request(request, (200, "DECIDED"), (200, "DECIDED"))
