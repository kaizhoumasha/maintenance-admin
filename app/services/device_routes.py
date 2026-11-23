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


async def authorize_new_command(session, owner, device_code: str, command_model: type) -> None:
    from app.businesses import catalog
    from app.integrations.ecs_command.declaration import command_declaration
    from app.models import WorkLine

    binding = await resolve_binding(session, device_code)
    if binding is None:
        raise ValueError("Business command requires a configured device binding")
    line = await session.get(WorkLine, binding.work_line_id, populate_existing=True)
    task_type = command_declaration(command_model).bindings[0].task_type
    if (
        line is None
        or line.business_code != owner.domain
        or not catalog.command_is_allowed(
            owner.domain, binding.device_role, command_model, task_type
        )
    ):
        raise ValueError("Business command is not authorized for this workline and contract")
