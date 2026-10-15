from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import HTTP_STATUS, AppError, ErrorCode, ErrorResponse
from .logger import endpoint_src, logger, origin_src
from .request_context import get_trace_id

# 由 HTTP_STATUS 反查,新增错误码只需在 errors.py 定义一次;未列出的状态码统一按 INVALID_REQUEST 处理
# shortcut: 反查要求状态码唯一(test_http_status_unique 保证),两个错误码共用一个状态码时改为显式映射
_HTTP_ERROR_CODE = {status: code for code, status in HTTP_STATUS.items()}

# Starlette 的 HTTPException.detail 是英文,面向用户的消息按错误码给中文
_HTTP_MESSAGE = {
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不允许",
}


def _src(request: Request, exc: BaseException) -> dict[str, str]:
    """日志位置列:优先异常实际抛出处,没有项目栈帧(如校验错误)时用命中的接口函数。"""
    return {"src": origin_src(exc) or endpoint_src(request.scope.get("endpoint"))}


def _error_response(
    status: int, code: ErrorCode, message: str, detail: str | None = None
) -> JSONResponse:
    """
    构建统一的错误响应。

    Args:
        status: HTTP 状态码。
        code: 错误码，用于客户端识别错误类型。
        message: 错误消息，面向用户的描述。
        detail: 可选的错误详情，提供更具体的错误信息（如参数校验失败的具体字段）。

    Returns:
        JSONResponse: 包含统一格式错误响应的 JSON 响应对象，带有 X-Trace-Id 响应头。
    """
    trace_id = get_trace_id()
    body = ErrorResponse(trace_id=trace_id, code=code, message=message, detail=detail)
    # 必须在这里设置响应头:Exception handler 由最外层的 ServerErrorMiddleware 调用,
    # 响应不经过 trace_id_middleware。其余 handler 会经过中间件,重复设置无害(值相同)。
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json"),
        headers={"X-Trace-Id": trace_id},
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """
    处理应用自定义业务异常。

    Args:
        request: FastAPI 请求对象。
        exc: 应用自定义异常，包含业务错误码、错误消息和可选详情。

    Returns:
        JSONResponse: 格式化的错误响应，4xx 错误仅记录警告日志，5xx 错误记录完整堆栈。
    """
    status = HTTP_STATUS[exc.code]
    # 4xx 是调用方的问题,不记堆栈,避免日志噪音
    logger.warning(
        "%s %s | %s %s",
        exc.code,
        exc.message,
        request.method,
        request.url.path,
        exc_info=exc if status >= 500 else None,
        extra=_src(request, exc),
    )
    return _error_response(status, exc.code, exc.message, exc.detail)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    处理请求参数校验异常。

    Args:
        request: FastAPI 请求对象。
        exc: 请求校验异常，包含所有校验失败的字段及其错误信息。

    Returns:
        JSONResponse: 参数错误响应，detail 字段包含所有校验失败的具体位置和原因。
    """
    detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    code = ErrorCode.INVALID_REQUEST
    logger.warning(
        "%s %s | %s %s",
        code,
        detail,
        request.method,
        request.url.path,
        extra=_src(request, exc),
    )
    return _error_response(HTTP_STATUS[code], code, "请求参数错误", detail)


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    处理 HTTP 异常（来自 Starlette 的 HTTPException）。

    Args:
        request: FastAPI 请求对象。
        exc: HTTP 异常，包含 HTTP 状态码和可选的详情信息。

    Returns:
        JSONResponse: 错误响应，将 404/405 映射为对应错误码，其余状态码统一按 INVALID_REQUEST 处理。
    """
    code = _HTTP_ERROR_CODE.get(exc.status_code, ErrorCode.INVALID_REQUEST)
    return _error_response(exc.status_code, code, _HTTP_MESSAGE.get(code, "请求错误"))


async def db_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    处理数据库异常，按类型映射错误码，并把定位信息写入日志。

    响应里只有 trace_id 和异常类型名；开发拿 trace_id 到日志里查 SQL、SQLSTATE、约束名和堆栈。
    不记录 SQL 参数，避免密码哈希、邮箱等敏感值进日志。
    """
    cause = getattr(exc, "orig", None) or exc
    cause = getattr(cause, "__cause__", None) or cause  # asyncpg 的原始异常挂在 __cause__ 上
    sql = " ".join((getattr(exc, "statement", None) or "").split())
    info = (
        f"sqlstate={getattr(cause, 'sqlstate', None)} "
        f"constraint={getattr(cause, 'constraint_name', None)} "
        f"orig={type(cause).__name__} sql={sql} | {request.method} {request.url.path}"
    )
    detail = type(exc).__name__
    if isinstance(exc, IntegrityError):
        # 唯一/外键/非空约束冲突:调用方的问题,不记堆栈
        logger.warning("database integrity error: %s", info, extra=_src(request, exc))
        code, message = ErrorCode.CONFLICT, "数据冲突，请检查是否重复或关联数据是否存在"
    elif isinstance(
        exc, (OperationalError, InterfaceError, PoolTimeoutError, ConnectionError, TimeoutError)
    ):
        # ConnectionError/TimeoutError:连不上库时 asyncpg 抛的是原生异常,不是 SQLAlchemyError
        # shortcut: OperationalError 也含死锁等,统一按 503;需区分时按 sqlstate 细分
        # shortcut: 原生 ConnectionError 一律视为数据库问题,接入其他外部服务后按来源区分
        logger.error("database unavailable: %s", info, exc_info=exc, extra=_src(request, exc))
        code, message = ErrorCode.DATABASE_UNAVAILABLE, "数据库暂不可用，请稍后重试"
    else:
        logger.error("database error: %s", info, exc_info=exc, extra=_src(request, exc))
        code, message = ErrorCode.INTERNAL_ERROR, "系统内部错误，请稍后重试"
    return _error_response(HTTP_STATUS[code], code, message, detail)


async def redis_error_handler(request: Request, exc: RedisError) -> JSONResponse:
    """Redis 不可用时认证无法完成,明确返回 503 而不是 500。"""
    logger.error(
        "redis unavailable | %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
        extra=_src(request, exc),
    )
    # shortcut: 与数据库不可用共用 503/DATABASE_UNAVAILABLE(状态码须唯一),需区分时新增错误码并改映射
    code = ErrorCode.DATABASE_UNAVAILABLE
    return _error_response(
        HTTP_STATUS[code], code, "缓存服务暂不可用，请稍后重试", type(exc).__name__
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    处理所有未被捕获的异常（兜底异常处理器）。

    Args:
        request: FastAPI 请求对象。
        exc: 未处理的异常对象。

    Returns:
        JSONResponse: 统一的内部错误响应，异常详情仅记录到日志，不返回给客户端。
    """
    # 异常详情只进日志,不返回给客户端
    logger.exception(
        "未处理异常 | %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
        extra=_src(request, exc),
    )
    return _error_response(
        HTTP_STATUS[ErrorCode.INTERNAL_ERROR], ErrorCode.INTERNAL_ERROR, "系统内部错误，请稍后重试"
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    注册全局异常处理器，将各类异常映射到统一的错误响应格式。

    Args:
        app: FastAPI 应用实例，用于注册异常处理器。

    Returns:
        None
    """
    app.add_exception_handler(
        AppError,
        app_error_handler,  # type: ignore[arg-type] -- handler 参数类型比 Exception 窄
    )
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type] -- handler 参数类型比 Exception 窄
    )
    app.add_exception_handler(
        StarletteHTTPException,
        http_error_handler,  # type: ignore[arg-type] -- handler 参数类型比 Exception 窄
    )
    app.add_exception_handler(
        SQLAlchemyError,
        db_error_handler,  # type: ignore[arg-type] -- handler 参数类型比 Exception 窄
    )
    app.add_exception_handler(ConnectionError, db_error_handler)
    app.add_exception_handler(TimeoutError, db_error_handler)
    app.add_exception_handler(RedisError, redis_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
