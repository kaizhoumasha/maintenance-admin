from fastapi import Request

from app.core.errors import AppError, ErrorCode
from app.core.request_context import get_trace_id
from app.services.asset_audit import AssetActor


def actor(request: Request):
    return AssetActor(
        request.state.audit_user_id,
        get_trace_id(),
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
    )


def device_status(request: Request):
    service = getattr(request.app.state, "device_status", None)
    if service is None:
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "设备状态服务尚未就绪")
    return service
