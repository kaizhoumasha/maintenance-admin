"""三类接入共用的文件名、来源和发现作用域;不参与协议或事务执行。"""

import inspect
import sys
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from app.core.module_tree import import_module_tree
from app.core.operation_names import is_operation_name

_local_only = ContextVar("integration_local_only", default=False)
_root = Path(__file__).resolve().parents[2]
_entrypoints = {"ecs_event", "callback", "request", "dev_ecs_event", "dev_callback"}


@contextmanager
def local_registration():
    """发现器显式标记仅本地加载的入口,异常时也恢复外层作用域。"""
    token = _local_only.set(True)
    try:
        yield
    finally:
        _local_only.reset(token)


def registration_filename(name: str) -> str:
    return name.lower().replace(".", "_").replace("@", "_") + ".py"


def import_registration_module(name: str, *, local_only: bool = False) -> None:
    with local_registration() if local_only else nullcontext():
        import_module_tree(name)


@dataclass(frozen=True)
class RegistrationSource:
    module: str
    source_file: str
    symbol: str
    local_only: bool

    @property
    def location(self) -> str:
        return f"{self.module}.{self.symbol} ({self.source_file})"


def registration_source(target) -> RegistrationSource:
    module = target.__module__
    path = getattr(sys.modules.get(module), "__file__", None)
    source_file = module.replace(".", "/") + ".py"
    if path:
        resolved = Path(path).resolve()
        source_file = str(resolved.relative_to(_root)) if resolved.is_relative_to(_root) else path
    return RegistrationSource(module, source_file, target.__qualname__, _local_only.get())


def validate_registration_file(name: str, target) -> RegistrationSource:
    source = registration_source(target)
    parts = source.module.split(".")
    if (
        parts[:2] == ["app", "integrations"]
        and len(parts) > 4
        and parts[3] in _entrypoints
        and parts[-1] + ".py" != registration_filename(name)
    ):
        raise ValueError(
            f"{source.location}: filename mismatch for {name!r}; "
            f"修复: 将文件重命名为 {registration_filename(name)}，一个注册项一个文件"
        )
    return source


def operation_source(name: str, target) -> RegistrationSource:
    if not isinstance(name, str) or not is_operation_name(name):
        source = registration_source(target)
        raise ValueError(
            f"{source.location}: invalid operation name {name!r}; "
            "修复: 使用小写点分名称及正整数版本，例如 example.done@v1，最长 256 字符"
        )
    return validate_registration_file(name, target)


def validate_function(target, data, *, optional_data: bool = False) -> None:
    if not inspect.iscoroutinefunction(target):
        raise TypeError(
            f"{registration_source(target).location}: 修复: 使用 async def handler(ctx, payload)"
        )
    parameters = inspect.signature(target).parameters.values()
    if len(parameters) != 2 or any(
        p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in parameters
    ):
        raise TypeError(
            f"{registration_source(target).location}: 修复: 函数必须接收 (ctx, payload)"
        )
    if data is None and optional_data:
        return
    if not (isinstance(data, type) and issubclass(data, BaseModel)):
        raise TypeError(
            f"{registration_source(target).location}: invalid data model; "
            "修复: data= 指定 Pydantic payload 模型"
        )
