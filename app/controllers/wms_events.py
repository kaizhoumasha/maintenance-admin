import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.core.database import session_factory
from app.core.ratelimit import RateLimit
from app.core.request_body import read_body_capped
from app.core.request_context import get_trace_id
from app.diagnostics import log_exception
from app.integrations.wms_callback import MAX_BODY_BYTES, CallbackResponse, receive, unavailable
from app.integrations.wms_callback.registry import operations
from app.integrations.wms_callback.snapshots import (
    SNAPSHOT_PREVIEW_BYTES,
    allow_snapshot,
    record_snapshot,
)

logger = logging.getLogger(__name__)
SAMPLE_OPERATION_ID = "019f12d0-58d7-7b4d-a23a-1b90aa5d4472"
SAMPLE_TIMESTAMP = 1786060800000
EXAMPLES: dict = {}
router = APIRouter(tags=["WMS callbacks"], dependencies=[Depends(RateLimit(None))])


@router.post(
    "/wms/events",
    status_code=422,
    response_class=Response,
    responses={
        status: {"description": description}
        for status, description in {
            400: "Invalid JSON or callback identity; empty body",
            409: "CONFLICT: identity already has different content",
            413: "Body exceeds 256 KiB; empty body",
            422: "REJECTED: invalid envelope or unsupported operation",
            503: "UNAVAILABLE: retry the original identity and content",
        }.items()
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["operation_id", "operation", "timestamp", "data"],
                        "properties": {
                            "operation_id": {"type": "string", "format": "uuid"},
                            "operation": {"type": "string"},
                            "timestamp": {"type": "integer", "minimum": 0},
                            "data": {"type": "object"},
                        },
                    },
                    "examples": EXAMPLES,  # 由 include_wms_events 按已注册 operation 填充
                }
            },
        }
    },
)
async def events(request: Request, background_tasks: BackgroundTasks) -> Response:
    received_at = datetime.now(UTC)
    body = b""
    factory = getattr(request.app.state, "callback_session_factory", session_factory)
    try:
        body = await read_body_capped(request, MAX_BODY_BYTES)
        if len(body) > MAX_BODY_BYTES:
            result = CallbackResponse(413, None, "too_large")
        else:
            async with factory() as session, session.begin():
                result = await receive(
                    session,
                    body,
                    runtime=getattr(request.app.state, "business_runtime", None),
                    on_committed=getattr(request.app.state, "business_event_committed", None),
                )
    except Exception as error:
        log_exception(
            logger, "callback receive failed", error, component="http", phase="wms_callback"
        )
        result = unavailable(body)
    if allow_snapshot():  # 被限流丢弃的请求不再把请求体挂到后台任务上
        background_tasks.add_task(
            record_snapshot,
            factory,
            preview=body[:SNAPSHOT_PREVIEW_BYTES],
            truncated=len(body) > SNAPSHOT_PREVIEW_BYTES,
            response=result,
            request_id=get_trace_id() or "",
            received_at=received_at,
        )
    if result.body is None:
        return Response(status_code=result.status_code, background=background_tasks)
    return JSONResponse(result.body, status_code=result.status_code, background=background_tasks)


def include_wms_events(
    app: FastAPI, *, environment: str, prefix: str = "/api/v1", runtime: object | None = None
) -> None:
    """挂载已完成正式加载、交叉校验及 local 加载的回调目录,并生成 Swagger 示例。"""
    if runtime is not None:
        app.state.business_runtime = runtime
    EXAMPLES.clear()
    for name, handler in operations().items():
        example = handler.example
        if example is None:
            continue
        EXAMPLES[name] = {
            "summary": handler.title,
            "value": {
                "operation_id": SAMPLE_OPERATION_ID,
                "operation": name,
                "timestamp": SAMPLE_TIMESTAMP,
                "data": example,
            },
        }
    app.include_router(router, prefix=prefix)
