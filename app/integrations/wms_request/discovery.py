"""自动发现请求 operation 模块:`app/integrations/<业务>/request` 在导入时由装饰器完成注册。

`request` 可以是单个模块,也可以是包(一个 operation 一个文件),包会连同全部子模块一起导入。
"""

import pkgutil
from functools import cache

from app import integrations
from app.core.module_tree import import_module_tree


def candidate_modules() -> list[str]:
    packages = sorted(
        info.name for info in pkgutil.iter_modules(integrations.__path__) if info.ispkg
    )
    return [f"{integrations.__name__}.{package}.request" for package in packages]


@cache
def load_operation_modules() -> None:
    for name in candidate_modules():
        import_module_tree(name)
