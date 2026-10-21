"""Trace ID 中间件:为每个请求生成 trace_id,写入上下文,通过 X-Trace-Id 响应头返回,并记录访问日志。"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app.core import logger, set_trace_id
from app.core.identifiers import uuid4
from app.core.logger import endpoint_src


def _log_access(request: Request, status: int, start: float) -> None:
    # 只记路径不记查询串:查询串可能带敏感信息
    # 按状态码分级,终端里 4xx 黄色、5xx 红色,一眼能扫到出问题的请求
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    logger.log(
        level,
        "%s %s -> %d %.1fms",
        request.method,
        request.url.path,
        status,
        (time.perf_counter() - start) * 1000,
        extra={"src": endpoint_src(request.scope.get("endpoint"))},
    )


async def trace_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    trace_id = uuid4().hex
    # 刻意不 reset:未处理异常的兜底 handler 在本函数退出之后才运行,仍要读到 trace_id。
    # uvicorn 每个请求一个独立 task,上下文互不串。
    set_trace_id(trace_id)
    try:
        response = await call_next(request)
    except Exception:
        # 兜底 handler 会把它变成 500,这里先记一行,保证每个请求都有一条访问日志
        _log_access(request, 500, start)
        raise
    response.headers["X-Trace-Id"] = trace_id
    _log_access(request, response.status_code, start)
    return response
