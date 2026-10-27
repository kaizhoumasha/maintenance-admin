"""Role contracts for new commands; current configuration gates every send."""

import re
from dataclasses import asdict, fields
from uuid import UUID

from app.integrations.ecs_command.contracts import PROTOCOL_RETRY_WINDOW, DeviceAnnex
from app.integrations.ecs_command.routing import (
    CommandBinding,
    RouteAuthorization,
    RouteSnapshot,
    normalize_origin,
)
from app.models import Device, DeviceRoute
from app.services.device_bindings import resolve_binding


class DeviceRoutes:
    def __init__(self, bindings=(), *, annexes=None):
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
        device = await session.get(Device, binding.device_id)
        route = await session.get(DeviceRoute, device.current_route_id)
        if route is None or route.device_code != binding.device_code:
            raise ValueError("Current device route is not configured")
        return RouteSnapshot(
            binding.device_code,
            route.ecs_origin,
            "device.route." + route.id,
            binding.revision,
            entry.annex_id,
            entry.annex_version,
            (entry.task_type,),
            PROTOCOL_RETRY_WINDOW,
        )

    async def resolve(self, session, device_code, task_type):
        binding = await resolve_binding(session, device_code)
        if binding is None:
            raise ValueError("Current device role is not configured")
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
        except (KeyError, TypeError, ValueError, AttributeError):
            return RouteAuthorization.UNKNOWN
        binding = await resolve_binding(session, frozen["device_code"])
        if binding is None:
            return RouteAuthorization.REVOKED
        entry = self.bindings.get((binding.device_role, commands[0]))
        if entry is None:
            return RouteAuthorization.REVOKED
        expected = asdict(await self._snapshot(session, binding, entry))
        expected["retry_window"] = PROTOCOL_RETRY_WINDOW.total_seconds()
        if {**frozen, "commands": tuple(commands)} != expected:
            return RouteAuthorization.REVOKED
        return RouteAuthorization.ALLOW


class DeviceCommandRoutes:
    def __init__(self, formal, debug):
        self.formal, self.debug = formal, debug

    @property
    def bindings(self):
        return self.formal.bindings

    async def resolve(self, session, device_code, task_type):
        return await self.formal.resolve(session, device_code, task_type)

    def for_origin(self, origin):
        return self.debug.for_origin(origin)

    async def authorize_frozen(self, session, route):
        if str(route.get("route_id", "")).startswith("device.route."):
            return await self.formal.authorize_frozen(session, route)
        return await self.debug.authorize_frozen(session, route)
