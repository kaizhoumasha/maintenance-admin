import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response

from app.core.database import session_factory
from app.core.ratelimit import RateLimit
from app.core.request_body import read_body_capped
from app.core.request_context import get_trace_id
from app.diagnostics import log_exception
from app.integrations.ecs_command.result import receive_ecs_result, reply
from app.integrations.wms_callback.snapshots import (
    SNAPSHOT_PREVIEW_BYTES,
    allow_snapshot,
    record_snapshot,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ECS callbacks"], dependencies=[Depends(RateLimit(None))])


@router.post(
    "/callback/result",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["command_code", "device_code", "result", "finish_time"],
                        "properties": {
                            "command_code": {"type": "string"},
                            "device_code": {"type": "string"},
                            "result": {"type": "string", "enum": ["SUCCESS", "FAILED"]},
                            "finish_time": {"type": "integer", "minimum": 0},
                            "data": {"type": "object"},
                            "error_detail": {
                                "anyOf": [
                                    {"type": "null"},
                                    {
                                        "type": "object",
                                        "required": ["code", "msg"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "code": {"type": "string"},
                                            "msg": {"type": "string"},
                                        },
                                    },
                                ]
                            },
                        },
                    }
                }
            },
        }
    },
    responses={
        400: {"description": "INVALID_ENVELOPE"},
        404: {"description": "COMMAND_NOT_FOUND"},
        409: {"description": "RESULT_BEFORE_DISPATCH or IDEMPOTENCY_CONFLICT"},
        413: {"description": "PAYLOAD_TOO_LARGE"},
        503: {"description": "TEMPORARILY_UNAVAILABLE"},
    },
)
async def result_callback(request: Request, background_tasks: BackgroundTasks):
    received_at = datetime.now(UTC)
    raw = b""
    body_available = False
    factory = getattr(request.app.state, "callback_session_factory", session_factory)
    try:
        raw = await read_body_capped(request, 262144)
        body_available = True
        if len(raw) > 262144:
            result = reply(413, "PAYLOAD_TOO_LARGE")
        else:
            service = getattr(request.app.state, "ecs_commands", None)
            if service is None:
                raise RuntimeError("ECS application assembly unavailable")
            async with factory() as session, session.begin():
                result = await receive_ecs_result(
                    session,
                    raw,
                    service,
                    on_result=getattr(request.app.state, "ecs_result_committed", None),
                )
    except Exception as error:
        log_exception(
            logger, "ECS result transaction failed", error, component="http", phase="ecs_result"
        )
        result = reply(503, "TEMPORARILY_UNAVAILABLE")
    if allow_snapshot("ecs_result"):
        background_tasks.add_task(
            record_snapshot,
            factory,
            preview=raw[:SNAPSHOT_PREVIEW_BYTES],
            truncated=len(raw) > SNAPSHOT_PREVIEW_BYTES,
            response=result,
            request_id=get_trace_id() or "",
            received_at=received_at,
            source="ecs_result",
            body_available=body_available,
        )
    return Response(
        result.prepared_body,
        status_code=result.status_code,
        media_type="application/json",
        background=background_tasks,
    )
