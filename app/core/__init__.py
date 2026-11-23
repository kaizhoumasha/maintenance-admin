import sys
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

from .config import Settings, get_settings
from .errors import HTTP_STATUS, AppError, ErrorCode, ErrorResponse, error_responses
from .request_context import get_trace_id, set_trace_id, trace_id_var
from .security import hash_password, verify_password

if TYPE_CHECKING:
    from .database import SessionDep
    from .deps import get_current_user, require_permission
    from .handlers import register_exception_handlers
    from .logger import logger, setup_logging


def __getattr__(name):
    modules = {
        "SessionDep": ".database",
        "get_current_user": ".deps",
        "require_permission": ".deps",
        "register_exception_handlers": ".handlers",
        "logger": ".logger",
        "setup_logging": ".logger",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value


class _CoreModule(ModuleType):
    def __setattr__(self, name, value):
        # Importing the logger submodule first must not replace the public Logger object.
        if name == "logger" and isinstance(value, ModuleType):
            value = value.logger
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CoreModule

__all__ = [
    "HTTP_STATUS",
    "AppError",
    "ErrorCode",
    "ErrorResponse",
    "error_responses",
    "SessionDep",
    "Settings",
    "get_settings",
    "get_trace_id",
    "get_current_user",
    "require_permission",
    "logger",
    "register_exception_handlers",
    "set_trace_id",
    "setup_logging",
    "trace_id_var",
    "hash_password",
    "verify_password",
]
