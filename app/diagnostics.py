"""安全异常诊断:不读取配置、不访问数据库,不管理重试或任务状态。"""

import logging
import re
import traceback
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

_APP_DIR = Path(__file__).resolve().parent
_PG_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]{0,62}"
_CONTEXT_FIELDS = {
    "task",
    "task_id",
    "message_id",
    "target",
    "source",
    "request_id",
    "retry_in_s",
    "method",
    "route",
    "src",
    "limit",
}


def error_cause(error: BaseException) -> BaseException:
    cause = getattr(error, "orig", None) or error
    return cause.__cause__ or cause


def diagnose(error: BaseException) -> dict[str, str]:
    fields = {"error_type": type(error).__name__}
    if isinstance(error, SQLAlchemyError):
        cause = error_cause(error)
        sqlstate = getattr(cause, "sqlstate", None)
        fields["driver_type"] = type(cause).__name__
        if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
            fields["sqlstate"] = sqlstate
        constraint = getattr(cause, "constraint_name", None)
        if isinstance(constraint, str) and re.fullmatch(_PG_IDENTIFIER, constraint):
            fields["constraint"] = constraint
        if fields.get("sqlstate") in {"42703", "42P01"}:
            # 只提取标准缺对象消息中的受限标识符,不回显任意驱动消息。
            missing = re.fullmatch(
                rf'(?:column|relation) (?P<quote>"?)'
                rf"(?P<name>{_PG_IDENTIFIER}(?:\.{_PG_IDENTIFIER}){{0,2}})"
                r"(?P=quote) does not exist",
                str(cause),
            )
            if missing:
                fields["missing"] = missing["name"]
            fields["hint"] = (
                "check database migrations; stop senders before uv run alembic upgrade head"
            )
    frame = project_frame(error)
    fields["origin"] = (
        f"{frame[0].relative_to(_APP_DIR.parent)}:{frame[1]}:{frame[2]}" if frame else "unavailable"
    )
    return fields


def project_frame(error: BaseException) -> tuple[Path, int, str] | None:
    found = None
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.is_relative_to(_APP_DIR):
            found = (path, line, frame.f_code.co_name)
    return found


def diagnostic_text(fields: Mapping[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in fields.items())


def safe_traceback(error: BaseException, tb=None) -> str:
    # 不格式化异常消息、异常链、源码或局部变量。
    stack = traceback.format_list(
        [
            (frame.f_code.co_filename, line, frame.f_code.co_name, "")
            for frame, line in traceback.walk_tb(tb if tb is not None else error.__traceback__)
        ]
    )
    return "Traceback (most recent call last):\n" + "".join(stack) + type(error).__name__


def log_exception(
    logger: logging.Logger,
    message: str,
    error: BaseException,
    *,
    component: str,
    phase: str,
    level: int = logging.ERROR,
    context: Mapping[str, object] | None = None,
    diagnostic: Mapping[str, str] | None = None,
    stack: bool = True,
) -> None:
    fields = dict(diagnostic if diagnostic is not None else diagnose(error))
    fields.update(component=component, phase=phase)
    fields.update(
        {
            key: value
            for key, value in (context or {}).items()
            if key in _CONTEXT_FIELDS and isinstance(value, (str, int, float, bool, type(None)))
        }
    )
    details = diagnostic_text(fields)
    logger.log(
        level,
        "%s %s%s",
        message,
        details,
        "\n" + safe_traceback(error) if stack else "",
        extra=fields,
        stacklevel=2,
    )
