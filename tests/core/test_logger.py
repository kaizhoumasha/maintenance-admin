import functools
import importlib
import logging
import sys

from app.core import security
from app.core.logger import DevFormatter, endpoint_src
from app.services import user_service

# app.core 导出了同名的 logger 对象,盖住了子模块,这里显式取模块
logger_mod = importlib.import_module("app.core.logger")


def _record_with_exc(level: int) -> logging.LogRecord:
    try:
        security.verify_password("x", "no-dollar-sign")  # 在 app 代码里抛 ValueError
    except ValueError:
        return logging.LogRecord("t", level, __file__, 1, "boom", None, sys.exc_info())
    raise AssertionError


def test_plain_format_has_origin_line():
    rec = _record_with_exc(logging.ERROR)
    rec.trace_id = "a" * 32
    out = DevFormatter(color=False).format(rec)
    assert "\x1b[" not in out
    assert "ERROR" in out and "aaaaaaaa" in out and "boom" in out
    assert "出错位置: app/core/security.py:" in out and "in verify_password" in out
    assert "Traceback" in out


def test_color_format_has_ansi():
    rec = _record_with_exc(logging.WARNING)
    assert "\x1b[33m" in DevFormatter(color=True).format(rec)


def test_src_overrides_logging_location():
    rec = logging.LogRecord("t", logging.INFO, "mw.py", 18, "GET / -> 200", None, None)
    rec.trace_id, rec.src = "b" * 32, "users.py:21"
    out = DevFormatter(color=False).format(rec)
    assert " users.py:21 " in out and "mw.py" not in out


def test_endpoint_src_handles_lambda_and_is_unambiguous():
    # lambda 没有 def 行,不能抛 StopIteration
    assert ":" in endpoint_src(lambda: 1)
    # 带目录,避免同名文件歧义
    assert endpoint_src(user_service.get_user).startswith("services/user_service.py:")


async def test_handler_log_points_to_raise_site(client, admin, caplog):
    await client.get("/api/v1/users/999", headers=admin)
    rec = next(
        r for r in caplog.records if r.levelname == "WARNING" and "NOT_FOUND" in r.getMessage()
    )
    assert rec.src.startswith("services/user_service.py:")


def test_uvicorn_loggers_use_root_handler(monkeypatch):
    uv = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(uv, "handlers", [logging.StreamHandler()])
    monkeypatch.setattr(uv, "propagate", False)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(logger_mod, "_done", False)
    logger_mod.setup_logging()
    assert uv.handlers == [] and uv.propagate


def test_endpoint_src_survives_missing_source():
    # 源码取不到(只有 .pyc、跨机器缓存的路径等)时不能抛异常,退回到代码对象记录的位置
    ns: dict = {}
    exec(compile("def f():\n    pass\n", "/nonexistent/dir/x.py", "exec"), ns)
    assert endpoint_src(ns["f"]) == "x.py:1"
    assert endpoint_src(functools.partial(print)) == "-"  # 没有 __code__ 的可调用对象
