import inspect
import logging
import os
import sys
import traceback
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.core.request_context import get_trace_id

_ROOT = Path(__file__).resolve().parents[2]
_APP = _ROOT / "app"
_RESET, _DIM = "\x1b[0m", "\x1b[90m"
_LEVEL_COLOR = {
    logging.DEBUG: "\x1b[90m",
    logging.INFO: "\x1b[32m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[1;97;41m",
}


def safe_db_exception(exc: BaseException) -> SQLAlchemyError:
    """驱动异常及异常链可能回显参数;只保留异常类型与原始栈位置。"""
    return SQLAlchemyError(type(exc).__name__).with_traceback(exc.__traceback__)


class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def _short(path: Path) -> str:
    """app 内的文件带目录(services/user_service.py)，避免同名文件歧义；其余只留文件名。"""
    return str(path.relative_to(_APP)) if path.is_relative_to(_APP) else path.name


def _innermost_app_frame(exc: BaseException) -> tuple[Path, int, str] | None:
    found = None
    for frame, lineno in traceback.walk_tb(exc.__traceback__):
        path = Path(frame.f_code.co_filename)
        if path.is_relative_to(_ROOT) and ".venv" not in path.parts:
            found = (path, lineno, frame.f_code.co_name)
    return found


def _origin(exc: BaseException) -> str | None:
    """异常最内层、属于本项目代码的栈帧，格式 `app/x.py:12 in func`；开发不用翻整段堆栈。"""
    frame = _innermost_app_frame(exc)
    return f"{frame[0].relative_to(_ROOT)}:{frame[1]} in {frame[2]}" if frame else None


def origin_src(exc: BaseException) -> str | None:
    """异常实际抛出处的 `目录/文件:行号`，用作日志的位置列。"""
    frame = _innermost_app_frame(exc)
    return f"{_short(frame[0])}:{frame[1]}" if frame else None


@lru_cache
def endpoint_src(endpoint: Callable | None) -> str:
    """命中的接口函数的 `目录/文件:def 所在行`；没匹配到路由（如 404）返回 `-`。"""
    if endpoint is None:
        return "-"
    code = getattr(endpoint, "__code__", None)
    if code is None:
        return "-"
    try:
        lines, start = inspect.getsourcelines(endpoint)
    except (OSError, TypeError):
        # 源码取不到(只有 .pyc、zip 部署等):退回到代码对象记录的位置。
        # 这个函数在每个请求的访问日志里调用,不能因为日志而让请求失败。
        return f"{_short(Path(code.co_filename))}:{code.co_firstlineno}"
    # lambda 等没有 def 行时退回到起始行
    offset = next(
        (i for i, ln in enumerate(lines) if ln.lstrip().startswith(("def ", "async def "))), 0
    )
    return f"{_short(Path(code.co_filename))}:{start + offset}"


class DevFormatter(logging.Formatter):
    """`时间 级别 trace_id前8位 文件:行号 消息`；异常时在堆栈前加一行出错位置。"""

    def __init__(self, color: bool) -> None:
        super().__init__()
        self.color = color

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    def format(self, record: logging.LogRecord) -> str:
        time_ = f"{self.formatTime(record, '%H:%M:%S')}.{int(record.msecs):03d}"
        level = self._paint(f"{record.levelname:<7}", _LEVEL_COLOR.get(record.levelno, ""))
        trace = getattr(record, "trace_id", "-")[:8].ljust(8)
        # 访问日志由中间件打印,调用位置没有信息量,中间件通过 extra["src"] 传入实际处理请求的接口位置
        where = getattr(record, "src", None) or f"{record.filename}:{record.lineno}"
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            msg = self._paint(msg, _LEVEL_COLOR[logging.ERROR])
        line = f"{self._paint(time_, _DIM)} {level} {self._paint(f'{trace} {where}', _DIM)}  {msg}"
        if record.exc_info and record.exc_info[1]:
            origin = _origin(record.exc_info[1])
            if origin:
                line += f"\n  ↳ 出错位置: {self._paint(origin, _LEVEL_COLOR[logging.WARNING])}"
            line += "\n" + self.formatException(record.exc_info)
        return line


_done = False


def setup_logging() -> None:
    global _done
    if _done:
        return

    # 只在终端里上色；重定向到文件或设置 NO_COLOR 时输出纯文本
    color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    handler = logging.StreamHandler()
    handler.setFormatter(DevFormatter(color))
    handler.addFilter(TraceIdFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # 访问日志改由 trace_id_middleware 记录(带 trace_id);uvicorn 自带的没有 trace_id,且会重复
    logging.getLogger("uvicorn.access").disabled = True
    # uvicorn 自己的日志(启动、重载、错误)也走同一个 handler,格式统一
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    _done = True


logger = logging.getLogger("maintenance-admin")
