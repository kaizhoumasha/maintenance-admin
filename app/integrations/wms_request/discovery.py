"""自动发现请求 operation 模块:`app/integrations/<业务>/request.py` 在导入时由装饰器完成注册。

模块自身的导入错误必须暴露,不能让 operation 静默缺失。
"""

import importlib
import pkgutil
from functools import cache

from app import integrations


def candidate_modules() -> list[str]:
    packages = sorted(
        info.name for info in pkgutil.iter_modules(integrations.__path__) if info.ispkg
    )
    return [f"{integrations.__name__}.{package}.request" for package in packages]


@cache
def load_operation_modules() -> None:
    for name in candidate_modules():
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
