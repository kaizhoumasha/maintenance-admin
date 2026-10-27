"""Authenticated ECS status query in every environment, without result persistence."""

import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Query, Request

from app.core import error_responses, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.routing import new_router
from app.http_client import AsyncHttpClient
from app.integrations.ecs_status import (
    CAPABILITY_NAME,
    StatusQueryError,
    StatusQueryIn,
    StatusQueryResult,
    query,
)
from app.integrations.ecs_status.client import STATUS_PATH
from app.integrations.registration import registration_source

router = new_router(
    prefix="/ecs/status",
    tags=["ecs-status"],
    dependencies=[Depends(require_permission("ecs:status:query"))],
)


def status_capability() -> dict:
    return {
        "kind": "ecs_status",
        "name": CAPABILITY_NAME,
        "entry_type": "builtin_capability",
        **asdict(registration_source(query)),
        "endpoint": None,
        "payload_schema": None,
        "method": "GET",
        "path": STATUS_PATH,
        "query_schema": StatusQueryIn.model_json_schema(),
        "response_schema": StatusQueryResult.model_json_schema(),
    }


@router.get("", response_model=StatusQueryResult, responses=error_responses(502, 504))
async def query_status(request: Request, inputs: Annotated[StatusQueryIn, Query()]):
    """查询指定 ECS 的全部或单台设备；结果只返回，不登记、不保存、不发命令。"""
    try:
        return await query(
            inputs.ecs_origin,
            device_code=inputs.device_code,
            client_factory=getattr(request.app.state, "ecs_status_client_factory", AsyncHttpClient),
        )
    except StatusQueryError as error:
        raise status_error(error) from None
    except ValueError:
        raise AppError(ErrorCode.INVALID_REQUEST, "ECS 地址或设备编码无效") from None


def status_error(error: StatusQueryError) -> AppError:
    headers = None
    if error.http_status == 429:
        code = ErrorCode.TOO_MANY_REQUESTS
        if error.retry_after_at is not None:
            seconds = max(0, math.ceil((error.retry_after_at - datetime.now(UTC)).total_seconds()))
            headers = {"Retry-After": str(seconds)}
    elif error.error_kind.endswith("TIMEOUT"):
        code = ErrorCode.UPSTREAM_TIMEOUT
    else:
        code = ErrorCode.UPSTREAM_ERROR
    detail = json.dumps(
        {
            "capability_name": error.capability_name,
            "ecs_origin": error.ecs_origin,
            "requested_device_code": error.requested_device_code,
            "completed_at": error.completed_at.isoformat(),
            "error_kind": error.error_kind,
            "http_status": error.http_status,
        },
        separators=(",", ":"),
    )
    return AppError(code, "ECS 状态查询失败", detail, headers)
