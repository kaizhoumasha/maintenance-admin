"""Model-only transactional ECS commands and automatic local protocol assembly.

Businesses declare @ecs_command(..., device_roles=(...)), list exact models in
composition.COMMANDS, and call create(..., command=Model(...)). The business
catalog compiles contracts internally; bindings and annexes are platform details.
Dynamic local HTTP JSON is parsed by the trusted adapter into the same core.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from .contracts import PROTOCOL_RETRY_WINDOW
from .declaration import compile_commands, ecs_command

if TYPE_CHECKING:
    from .roles import RoleRoutes
    from .runtime import configure_ecs
    from .service import CommandOwner, EcsCommands


def __getattr__(name):
    modules = {
        "RoleRoutes": ".roles",
        "configure_ecs": ".runtime",
        "CommandOwner": ".service",
        "EcsCommands": ".service",
    }
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "PROTOCOL_RETRY_WINDOW",
    "CommandOwner",
    "EcsCommands",
    "RoleRoutes",
    "configure_ecs",
    "compile_commands",
    "ecs_command",
]
