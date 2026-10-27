"""Transactional ECS commands with formal routes and automatic local protocol assembly."""

from importlib import import_module
from typing import TYPE_CHECKING

from .contracts import PROTOCOL_RETRY_WINDOW, CommandBinding, DeviceAnnex

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
    "CommandBinding",
    "CommandOwner",
    "DeviceAnnex",
    "EcsCommands",
    "RoleRoutes",
    "configure_ecs",
]
