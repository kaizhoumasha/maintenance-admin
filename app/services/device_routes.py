"""Workline/database adapter for the independent ECS role routes."""

from app.integrations.ecs_command import RoleRoutes
from app.models import Device, DeviceRoute
from app.services.device_bindings import resolve_binding


async def resolve_route(session, binding):
    device = await session.get(Device, binding.device_id)
    route = await session.get(DeviceRoute, device.current_route_id)
    if route is None or route.device_code != binding.device_code:
        raise ValueError("Current device route is not configured")
    return route.ecs_origin, "device.route." + route.id


class DeviceRoutes(RoleRoutes):
    def __init__(self, bindings=(), *, annexes=None):
        super().__init__(
            bindings,
            annexes=annexes,
            resolve_binding=resolve_binding,
            resolve_route=resolve_route,
        )
