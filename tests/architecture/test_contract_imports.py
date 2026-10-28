import os
import subprocess
import sys
from pathlib import Path

import pytest

SETTINGS = {
    "APP_NAME": "contract-import-probe",
    "ENVIRONMENT": "production",
    "DATABASE_URL": "sqlite+aiosqlite://",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "JWT_SECRET_KEY": "test-secret-test-secret-test-secret-32",
}

PUBLIC_EXPORTS = {
    "HTTP_STATUS": "errors",
    "AppError": "errors",
    "ErrorCode": "errors",
    "ErrorResponse": "errors",
    "error_responses": "errors",
    "SessionDep": "database",
    "Settings": "config",
    "get_settings": "config",
    "get_trace_id": "request_context",
    "get_current_user": "deps",
    "require_permission": "deps",
    "logger": "logger",
    "register_exception_handlers": "handlers",
    "set_trace_id": "request_context",
    "setup_logging": "logger",
    "trace_id_var": "request_context",
    "hash_password": "security",
    "verify_password": "security",
}

NO_NETWORK = """
import socket

def forbidden(*args, **kwargs):
    raise AssertionError("network connection during import")

socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
"""


def _cold_process(tmp_path, code, *, settings=None):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1])\n" + code,
            str(Path(__file__).resolve().parents[2]),
        ],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], **(settings or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("order", ["public_first", "submodule_first", "star"])
def test_core_public_exports_keep_identity_in_both_import_orders(tmp_path, order):
    _cold_process(
        tmp_path,
        NO_NETWORK
        + f"""
from importlib import import_module
import logging

exports = {PUBLIC_EXPORTS!r}
order = {order!r}
if order == "submodule_first":
    logger_module = import_module("app.core.logger")
    from app.core import logger
    assert logger is logger_module.logger
    for module in set(exports.values()):
        import_module("app.core." + module)

import app.core as core
assert set(core.__all__) == set(exports)
namespace = {{}}
if order == "star":
    exec("from app.core import *", namespace)
else:
    for name in exports:
        exec("from app.core import " + name, namespace)

for name, module in exports.items():
    original = getattr(import_module("app.core." + module), name)
    assert namespace[name] is original, name
    assert getattr(core, name) is original, name
assert isinstance(core.logger, logging.Logger)
assert core.logger.name == core.get_settings().APP_NAME == "contract-import-probe"
""",
        settings=SETTINGS,
    )


@pytest.mark.parametrize("entry", ["app.main", "app.tasks.celery_app"])
def test_api_and_worker_bootstrap_keep_public_core_exports(tmp_path, entry):
    _cold_process(
        tmp_path,
        NO_NETWORK
        + f"""
from importlib import import_module
import logging

entry = import_module({entry!r})
if {entry!r} == "app.main":
    assert entry.app.title == "contract-import-probe"
else:
    assert entry.celery_app.main == "contract-import-probe"
    entry.celery_app.loader.import_default_modules()

import app.core as core
for name, module in {PUBLIC_EXPORTS!r}.items():
    assert getattr(core, name) is getattr(import_module("app.core." + module), name), name
assert isinstance(core.logger, logging.Logger)
assert core.logger.name == core.get_settings().APP_NAME == "contract-import-probe"
""",
        settings=SETTINGS,
    )


@pytest.mark.parametrize("name", ["SessionDep", "logger"])
def test_runtime_exports_still_require_settings(tmp_path, name):
    _cold_process(
        tmp_path,
        f"""
from pydantic import ValidationError
try:
    from app.core import {name}
except ValidationError as error:
    assert {{item["loc"] for item in error.errors()}} == {{
        ("DATABASE_URL",), ("REDIS_URL",), ("JWT_SECRET_KEY",)
    }}
else:
    raise AssertionError("runtime export must validate required settings")
""",
    )


def test_contract_discovery_does_not_initialize_runtime_resources(tmp_path):
    _cold_process(
        tmp_path,
        """
import importlib.abc
import socket

import httpx
import redis
import redis.asyncio
import sqlalchemy
import sqlalchemy.ext.asyncio
from unittest.mock import patch

def forbidden(*args, **kwargs):
    raise AssertionError("runtime resource construction or network connection")

class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith((
            "app.core.database", "app.core.redis", "app.core.deps",
            "app.core.logger", "app.main", "app.controllers", "app.tasks",
            "app.reliable_runtime", "app.businesses.runtime",
            "app.integrations.ecs_command.runtime",
        )):
            raise AssertionError("runtime import: " + fullname)

sys.meta_path.insert(0, BlockRuntime())
with (
    patch.object(sqlalchemy, "create_engine", forbidden),
    patch.object(sqlalchemy.engine, "create_engine", forbidden),
    patch.object(sqlalchemy.ext.asyncio, "create_async_engine", forbidden),
    patch.object(sqlalchemy.engine.Engine, "__init__", forbidden),
    patch.object(httpx.Client, "__init__", forbidden),
    patch.object(httpx.AsyncClient, "__init__", forbidden),
    patch.object(redis.Redis, "__init__", forbidden),
    patch.object(redis.asyncio.Redis, "__init__", forbidden),
    patch.object(socket.socket, "connect", forbidden),
    patch.object(socket.socket, "connect_ex", forbidden),
    patch.object(socket, "create_connection", forbidden),
):
    import app.database
    app.database.create_database_engine = forbidden
    from app.businesses import catalog
    from app.integrations.discovery import load_formal_modules
    from app.integrations.wms_callback.registry import operations

    load_formal_modules()
    assert catalog.validate_business_contracts() == ((), {})
    assert catalog.definitions() == ()
    assert "transport.task.resulted@v1" in operations()
""",
    )
