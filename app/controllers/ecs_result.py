import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.database import session_factory
from app.core.ratelimit import RateLimit
from app.core.request_body import read_body_capped
from app.integrations.ecs_command.result import receive_ecs_result, reply

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
async def result_callback(request: Request):
    try:
        raw = await read_body_capped(request, 262144)
        if len(raw) > 262144:
            result = reply(413, "PAYLOAD_TOO_LARGE")
        else:
            service = getattr(request.app.state, "ecs_commands", None)
            if service is None:
                raise RuntimeError("ECS application assembly unavailable")
            factory = getattr(request.app.state, "callback_session_factory", session_factory)
            async with factory() as session, session.begin():
                result = await receive_ecs_result(session, raw, service)
    except Exception as error:
        logger.error("ECS result transaction failed error_type=%s", type(error).__name__)
        result = reply(503, "TEMPORARILY_UNAVAILABLE")
    return JSONResponse(result.body, status_code=result.status_code)
