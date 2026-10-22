"""自动发现入站 operation 模块:`app/integrations/<业务>/callback.py` 所有环境导入,
`dev_callback.py` 仅 `ENVIRONMENT=local` 导入;导入时由 `@callback_operation` 完成注册。

沿用联调控制台 `dev.py` 的扫描方式:模块自身的导入错误必须暴露,不能让 operation 静默缺失。
"""

import importlib
import pkgutil

from app import integrations


def candidate_modules(environment: str) -> list[str]:
    suffixes = ["callback"] + (["dev_callback"] if environment == "local" else [])
    packages = sorted(
        info.name for info in pkgutil.iter_modules(integrations.__path__) if info.ispkg
    )
    return [
        f"{integrations.__name__}.{package}.{suffix}" for package in packages for suffix in suffixes
    ]


def load_operation_modules(environment: str) -> None:
    for name in candidate_modules(environment):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
