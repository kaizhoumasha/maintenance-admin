"""统一错误处理：错误码、HTTP 状态码映射、错误响应模型和自定义异常。"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"


# 错误码到 HTTP 状态码的映射表；handlers.py 按状态码反查，所以状态码必须唯一
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONFLICT: 409,
    ErrorCode.TOO_MANY_REQUESTS: 429,
    ErrorCode.DATABASE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
}


class ErrorResponse(BaseModel):
    trace_id: str
    code: ErrorCode
    message: str
    detail: str | None


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """路由 responses= 的简写:error_responses(400, 404) → 这些状态码都用 ErrorResponse。"""
    return {status: {"model": ErrorResponse} for status in statuses}


class AppError(Exception):
    """业务异常：服务层一律抛它，由全局 handler 转成 ErrorResponse。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.headers = headers
