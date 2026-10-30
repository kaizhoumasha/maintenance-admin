import logging
from datetime import UTC, datetime
from time import perf_counter

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.core.database import session_factory
from app.core.ratelimit import RateLimit
from app.core.request_body import read_body_capped
from app.core.request_context import get_trace_id
from app.core.strict_json import json_object
from app.diagnostics import log_exception
from app.integrations.ecs_callback.binding import BindingResolver
from app.integrations.ecs_callback.receiver import (
    MAX_BODY_BYTES,
    EcsResponse,
    receive_ecs_event,
    render_untrusted,
)
from app.integrations.ecs_callback.registry import events
from app.integrations.wms_callback.snapshots import (
    SNAPSHOT_PREVIEW_BYTES,
    allow_snapshot,
    record_snapshot,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ECS callbacks"], dependencies=[Depends(RateLimit(None))])


@router.post(
    "/callback/event",
    response_class=Response,
    responses={
        400: {"description": "INVALID_ENVELOPE: bad JSON or envelope; see error_detail.issues"},
        413: {"description": "PAYLOAD_TOO_LARGE: body exceeds 256 KiB"},
        503: {"description": "TEMPORARILY_UNAVAILABLE: retry the original event"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["device_code", "event_type", "timestamp"],
                        "properties": {
                            "device_code": {"type": "string"},
                            "event_type": {"type": "string"},
                            "timestamp": {"type": "integer", "minimum": 0},
                            "data": {"type": "object"},
                        },
                    }
                }
            },
        }
    },
)
async def event_callback(request: Request, background_tasks: BackgroundTasks) -> Response:
    result = await receive_event_request(request, background_tasks)
    return JSONResponse(result.body, status_code=result.status_code, background=background_tasks)


async def receive_event_request(request: Request, background_tasks: BackgroundTasks) -> EcsResponse:
    started = perf_counter()
    received_at = datetime.now(UTC)
    factory = getattr(request.app.state, "callback_session_factory", session_factory)
    raw = b""
    try:
        raw = await read_body_capped(request, MAX_BODY_BYTES)
        if len(raw) > MAX_BODY_BYTES:
            result = EcsResponse(413, {"code": 413, "message": "PAYLOAD_TOO_LARGE"}, "too_large")
        else:
            async with factory() as session, session.begin():
                result = await receive_ecs_event(
                    session,
                    raw,
                    resolve_binding=getattr(request.app.state, "ecs_event_binding_resolver", None),
                    runtime=getattr(request.app.state, "business_runtime", None),
                    on_committed=getattr(request.app.state, "business_event_committed", None),
                )
    except Exception as error:  # 含提交失败:整个事务已回滚
        log_exception(
            logger, "ecs callback receive failed", error, component="http", phase="ecs_event"
        )
        event_type, device_code = _best_effort_identity(raw)
        result = EcsResponse(
            503,
            {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"},
            "error",
            event_type,
            device_code,
        )
    _log_request(result, started)
    if allow_snapshot("ecs"):  # 被限流丢弃的请求不再把请求体挂到后台任务上
        background_tasks.add_task(
            record_snapshot,
            factory,
            preview=raw[:SNAPSHOT_PREVIEW_BYTES],
            truncated=len(raw) > SNAPSHOT_PREVIEW_BYTES,
            response=result,
            request_id=get_trace_id() or "",
            received_at=received_at,
            source="ecs",
        )
    return result


def _best_effort_identity(raw: bytes) -> tuple[str | None, str | None]:
    """仅供 503 日志:尽力从请求体取标识,任何失败都当作没有,绝不影响响应。"""
    try:
        parsed = json_object(raw) or {}
        event_type, device_code = parsed.get("event_type"), parsed.get("device_code")
        return (
            event_type if isinstance(event_type, str) else None,
            device_code if isinstance(device_code, str) else None,
        )
    except Exception:
        return None, None


def _log_request(result: EcsResponse, started: float) -> None:
    """事务提交/异常映射之后的唯一一行请求日志,outcome 是最终结果。"""
    fields = {
        "event_type": render_untrusted(result.event_type) if result.event_type else None,
        "device_code": render_untrusted(result.device_code) if result.device_code else None,
        "outcome": result.outcome,
        "digest": result.digest[:8] if result.digest else None,
        "status": result.status_code,
        "duration_ms": round((perf_counter() - started) * 1000, 1),
    }
    shown = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    logger.info("ecs callback %s", shown, extra=fields)


def include_ecs_callback(
    app: FastAPI,
    *,
    environment: str,
    prefix: str = "/api/v1",
    resolve_binding: BindingResolver | None = None,
    runtime: object | None = None,
) -> None:
    """Mount after the caller's formal loading, contract validation and local loading."""
    if runtime is not None:
        app.state.business_runtime = runtime
    if resolve_binding is None and any(
        not item.registration_source.local_only for item in events().values()
    ):
        raise ValueError("Formal ECS event handlers require a binding resolver")
    if environment == "local":
        from app.integrations.ecs_callback.dev_ecs_event import local_bindings

        resolve_binding = local_bindings(resolve_binding)
    app.state.ecs_event_binding_resolver = resolve_binding
    app.include_router(router, prefix=prefix)
