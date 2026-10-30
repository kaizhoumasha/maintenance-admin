"""One locked binding resolver shared by event dispatch and command routing."""

from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.integrations.ecs_callback.binding import DeviceBinding
from app.models import Device, WorkLine


async def resolve_binding(session, device_code: str) -> DeviceBinding | None:
    candidate = (
        await session.execute(
            select(Device.id, Device.work_line_id, Device.version).where(
                Device.device_code == device_code
            )
        )
    ).one_or_none()
    if candidate is None:
        return None
    line = None
    if candidate.work_line_id is not None:
        line = await session.scalar(
            select(WorkLine)
            .where(WorkLine.id == candidate.work_line_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    device = await session.scalar(
        select(Device)
        .where(Device.id == candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        device is None
        or device.work_line_id != candidate.work_line_id
        or device.version != candidate.version
        or (candidate.work_line_id is not None and line is None)
    ):
        raise AppError(ErrorCode.CONFLICT, "设备绑定已变化，请重试")
    if line is None:
        return None
    roles = [role for role, code in line.device_bindings.items() if code == device_code]
    if len(roles) != 1:
        return None
    return DeviceBinding(
        device_id=device.id,
        device_code=device.device_code,
        device_version=device.version,
        work_line_id=line.id,
        work_line_version=line.version,
        device_role=roles[0],
        device_bindings=dict(line.device_bindings),
        position_bindings=dict(line.position_bindings),
        business_code=line.business_code,
    )


async def resolve_role(session, work_line_id: int, device_role: str) -> DeviceBinding:
    line = await session.get(WorkLine, work_line_id, populate_existing=True)
    code = line.device_bindings.get(device_role) if line else None
    binding = await resolve_binding(session, code) if code else None
    if (
        binding is None
        or binding.work_line_id != work_line_id
        or binding.device_role != device_role
    ):
        raise AppError(ErrorCode.CONFLICT, "工作线角色未绑定或绑定已变化")
    return binding
