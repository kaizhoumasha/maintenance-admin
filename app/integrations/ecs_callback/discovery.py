"""自动发现 ECS 事件处理器模块:`app/integrations/<业务>/ecs_event` 所有环境导入,
`dev_ecs_event` 仅 `ENVIRONMENT=local` 导入;导入时由 `@ecs_event` 完成注册。
`ecs_event` 可以是单个模块,也可以是包(一个事件一个文件)。
"""

import pkgutil

from app import integrations
from app.integrations.registration import import_registration_module


def candidate_modules(environment: str) -> list[str]:
    suffixes = ["ecs_event"] + (["dev_ecs_event"] if environment == "local" else [])
    packages = sorted(
        info.name for info in pkgutil.iter_modules(integrations.__path__) if info.ispkg
    )
    return [
        f"{integrations.__name__}.{package}.{suffix}" for package in packages for suffix in suffixes
    ]


def load_event_modules(environment: str) -> None:
    for name in candidate_modules(environment):
        import_registration_module(name, local_only=name.rsplit(".", 1)[-1] == "dev_ecs_event")
