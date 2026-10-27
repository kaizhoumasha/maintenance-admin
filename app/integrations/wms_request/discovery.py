"""自动发现请求 operation 模块:`app/integrations/<业务>/request` 在导入时由装饰器完成注册。

`request` 可以是单个模块,也可以是包(一个 operation 一个文件),包会连同全部子模块一起导入。
"""

import pkgutil

from app import integrations


def candidate_modules(environment: str = "production") -> list[str]:
    packages = sorted(
        info.name for info in pkgutil.iter_modules(integrations.__path__) if info.ispkg
    )
    suffixes = ["request"] + (["dev"] if environment == "local" else [])
    return [
        f"{integrations.__name__}.{package}.{suffix}" for package in packages for suffix in suffixes
    ]


def load_operation_modules() -> None:
    from app.integrations.discovery import load_formal_modules

    load_formal_modules(kind="request")
