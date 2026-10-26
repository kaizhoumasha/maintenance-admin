"""Static role contracts with injected runtime bindings and current-route authorization."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, fields
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.ecs_callback.binding import BindingResolver, DeviceBinding
from app.integrations.ecs_command.contracts import PROTOCOL_RETRY_WINDOW, DeviceAnnex
from app.integrations.ecs_command.routing import (
    CommandBinding,
    RouteAuthorization,
    RouteSnapshot,
    normalize_origin,
)


class RoleRoutes:
    def __init__(
        self,
        bindings=(),
        *,
        annexes=None,
        resolve_binding: BindingResolver,
        resolve_route: Callable[[AsyncSession, DeviceBinding], Awaitable[tuple[str, str]]],
    ):
        self._resolve_binding = resolve_binding
        self._resolve_route = resolve_route
        self.bindings = {}
        for entry in bindings:
            if not isinstance(entry, CommandBinding):
                raise TypeError("Declare CommandBinding entries by device_role and task_type")
            key = (entry.device_role, entry.task_type)
            if key in self.bindings:
                raise ValueError(
                    f"duplicate command binding {key}: "
                    f"{self.bindings[key].registration_source.location} and "
                    f"{entry.registration_source.location}; 修复: 每个角色/任务只声明一次"
                )
            if not isinstance(
                (annexes or {}).get((entry.annex_id, entry.annex_version)), DeviceAnnex
            ):
                raise ValueError(f"{entry.registration_source.location}: confirmed annex required")
            self.bindings[key] = entry

    async def _snapshot(self, session, binding, entry: CommandBinding) -> RouteSnapshot:
        origin, route_id = await self._resolve_route(session, binding)
        return RouteSnapshot(
            binding.device_code,
            origin,
            route_id,
            binding.revision,
            entry.annex_id,
            entry.annex_version,
            (entry.task_type,),
            PROTOCOL_RETRY_WINDOW,
        )

    async def resolve(self, session, device_code, task_type):
        binding = await self._resolve_binding(session, device_code)
        if binding is None:
            raise ValueError("Current device role is not configured")
        if binding.device_code != device_code:
            raise ValueError("ECS binding does not match command device")
        entry = self.bindings.get((binding.device_role, task_type))
        if entry is None:
            raise ValueError("Role command annex is not configured")
        return await self._snapshot(session, binding, entry)

    async def authorize_frozen(self, session, frozen):
        try:
            if not isinstance(frozen, dict) or set(frozen) != {
                f.name for f in fields(RouteSnapshot)
            }:
                return RouteAuthorization.UNKNOWN
            key = frozen["route_id"]
            version = frozen["version"]
            commands = frozen["commands"]
            if (
                not isinstance(key, str)
                or not key.startswith("device.route.")
                or not isinstance(version, str)
                or not re.fullmatch(r"2:[0-9a-f]{64}", version)
                or not isinstance(commands, (tuple, list))
                or len(commands) != 1
                or not isinstance(commands[0], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", commands[0])
                or any(
                    not isinstance(frozen[name], str) or not frozen[name]
                    for name in ("device_code", "origin", "annex_id", "annex_version")
                )
                or type(frozen["retry_window"]) not in (int, float)
                or frozen["retry_window"] != PROTOCOL_RETRY_WINDOW.total_seconds()
            ):
                return RouteAuthorization.UNKNOWN
            identity = key.removeprefix("device.route.")
            if normalize_origin(frozen["origin"]) != frozen["origin"]:
                return RouteAuthorization.UNKNOWN
            identity_uuid = UUID(identity)
            if str(identity_uuid) != identity or identity_uuid.version != 7:
                return RouteAuthorization.UNKNOWN
        except (KeyError, TypeError, ValueError, AttributeError, httpx.InvalidURL):
            return RouteAuthorization.UNKNOWN
        binding = await self._resolve_binding(session, frozen["device_code"])
        if binding is None:
            return RouteAuthorization.REVOKED
        if binding.device_code != frozen["device_code"]:
            return RouteAuthorization.UNKNOWN
        entry = self.bindings.get((binding.device_role, commands[0]))
        if entry is None:
            return RouteAuthorization.REVOKED
        expected = asdict(await self._snapshot(session, binding, entry))
        expected["retry_window"] = PROTOCOL_RETRY_WINDOW.total_seconds()
        if {**frozen, "commands": tuple(commands)} != expected:
            return RouteAuthorization.REVOKED
        return RouteAuthorization.ALLOW
