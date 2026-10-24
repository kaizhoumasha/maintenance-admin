"""自动发现入站 operation 模块:`app/integrations/<业务>/callback` 所有环境导入,
`dev_callback` 仅 `ENVIRONMENT=local` 导入;导入时由 `@callback_operation` 完成注册。

`callback` 可以是单个模块,也可以是包(一个 operation 一个文件),包会连同全部子模块一起导入。
"""

import pkgutil

from app import integrations
from app.integrations.registration import import_registration_module


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
        import_registration_module(name, local_only=name.rsplit(".", 1)[-1] == "dev_callback")
