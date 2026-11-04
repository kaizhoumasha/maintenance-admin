from collections.abc import Callable

from sqlalchemy import delete, func, select

from app.businesses.definitions import BusinessDefinition
from app.core.errors import AppError, ErrorCode
from app.integrations.ecs_callback.binding import DeviceBinding
from app.models import Device, WorkLine, WorkLinePosition
from app.schemas.workline import WorkLinePositionRead, WorkLineRead
from app.services.asset_audit import mutation, record_change
from app.services.pagination import paginate


async def lock_worklines(session, ids):
    rows = list(
        await session.scalars(
            select(WorkLine)
            .where(WorkLine.id.in_(set(ids)))
            .order_by(WorkLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if {row.id for row in rows} != set(ids):
        raise AppError(ErrorCode.NOT_FOUND, "工作线不存在")
    return rows


def snapshot(row, positions=()):
    return {
        "positions": [
            WorkLinePositionRead.model_validate(p).model_dump(mode="json") for p in positions
        ],
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "line_code",
                "line_name",
                "business_code",
                "is_enabled",
                "zone_name",
                "description",
                "sort_order",
                "version",
                "device_bindings",
                "position_bindings",
            )
        },
    }


async def position_rows(session, line_ids):
    rows = await session.scalars(
        select(WorkLinePosition)
        .where(WorkLinePosition.work_line_id.in_(line_ids))
        .order_by(WorkLinePosition.position_code)
    )
    result = {key: [] for key in line_ids}
    for row in rows:
        result[row.work_line_id].append(row)
    return result


async def read_workline(session, workline_id):
    row = await session.get(WorkLine, workline_id, populate_existing=True)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "工作线不存在")
    count = await session.scalar(
        select(func.count()).select_from(Device).where(Device.work_line_id == row.id)
    )
    positions = (await position_rows(session, [row.id]))[row.id]
    return WorkLineRead.model_validate(row).model_copy(
        update={
            "device_count": count,
            "positions": [WorkLinePositionRead.model_validate(p) for p in positions],
        }
    )


async def list_worklines(session, params, filters=None):
    from app.services.filtering import FieldSpec, apply_filter

    stmt = (
        apply_filter(
            select(WorkLine),
            filters,
            {
                "line_code": FieldSpec(WorkLine.line_code, str, ("eq", "ilike", "in")),
                "line_name": FieldSpec(WorkLine.line_name, str, ("eq", "ilike")),
                "business_code": FieldSpec(
                    WorkLine.business_code, str, ("eq", "in", "is_null", "not_null")
                ),
                "zone_name": FieldSpec(WorkLine.zone_name, str, ("eq", "ilike", "is_null")),
            },
        )
        .order_by(WorkLine.sort_order, WorkLine.id)
        .execution_options(populate_existing=True)
    )
    page = await paginate(session, stmt, params)
    if not page["items"]:
        return page
    counts = dict(
        (
            await session.execute(
                select(Device.work_line_id, func.count())
                .where(Device.work_line_id.in_([row.id for row in page["items"]]))
                .group_by(Device.work_line_id)
            )
        ).all()
    )
    positions = await position_rows(session, [row.id for row in page["items"]])
    page["items"] = [
        WorkLineRead.model_validate(row).model_copy(
            update={
                "device_count": counts.get(row.id, 0),
                "positions": [WorkLinePositionRead.model_validate(p) for p in positions[row.id]],
            }
        )
        for row in page["items"]
    ]
    return page


async def create_workline(session, data, *, actor, definition_lookup):
    async with mutation(session):
        definition = _definition(data.business_code, definition_lookup)
        _validate_positions(data.position_bindings, definition)
        row = WorkLine(**data.model_dump(exclude={"positions"}))
        session.add(row)
        await session.flush()
        await validate_bindings(session, row.id, row.device_bindings, definition=definition)
        positions = await _save_positions(
            session,
            row,
            definition,
            data.positions if "positions" in data.model_fields_set else None,
            creating=True,
        )
        await record_change(
            session,
            actor=actor,
            action="asset:workline:create",
            resource="work-lines",
            resource_id=row.id,
            before={},
            after=snapshot(row, positions),
        )
        row_id = row.id
    return await read_workline(session, row_id)


async def update_workline(session, workline_id, data, *, actor, definition_lookup):
    async with mutation(session):
        row = (await lock_worklines(session, [workline_id]))[0]
        if row.version != data.version:
            raise AppError(ErrorCode.CONFLICT, "工作线版本已变化")
        original_positions = (await position_rows(session, [row.id]))[row.id]
        before = snapshot(row, original_positions)
        updates = data.model_dump(exclude={"version", "positions"})
        for key in (
            "business_code",
            "is_enabled",
            "device_bindings",
            "position_bindings",
        ):
            if key not in data.model_fields_set:
                updates.pop(key)
        business_code = updates.get("business_code", row.business_code)
        if row.business_code is not None and business_code != row.business_code:
            raise AppError(ErrorCode.CONFLICT, "已选择的业务不能切换或清空")
        definition = _definition(business_code, definition_lookup) if business_code else None
        _validate_positions(updates.get("position_bindings", row.position_bindings), definition)
        await validate_bindings(
            session,
            row.id,
            updates.get("device_bindings", row.device_bindings),
            definition=definition,
        )
        next_bindings = updates.get("position_bindings", row.position_bindings)
        changed = {
            code for role, code in row.position_bindings.items() if next_bindings.get(role) != code
        }
        await _protect_positions(session, row.id, changed)
        for key, value in updates.items():
            setattr(row, key, value)
        positions = await _save_positions(
            session,
            row,
            definition,
            data.positions if "positions" in data.model_fields_set else None,
        )
        row.version += 1
        await record_change(
            session,
            actor=actor,
            action="asset:workline:update",
            resource="work-lines",
            resource_id=row.id,
            before=before,
            after=snapshot(row, positions),
        )
    return await read_workline(session, workline_id)


def _definition(code, definition_lookup):
    try:
        return definition_lookup(code)
    except LookupError:
        raise AppError(ErrorCode.INVALID_REQUEST, "业务未部署或不存在") from None


def _validate_positions(bindings, definition):
    if definition is not None and bindings.keys() - {p.slot_key for p in definition.position_slots}:
        raise AppError(ErrorCode.INVALID_REQUEST, "位置绑定不属于所选业务声明")


async def validate_bindings(session, workline_id, bindings, *, definition=None):
    if definition is not None and bindings.keys() - {r.role_key for r in definition.device_roles}:
        raise AppError(ErrorCode.INVALID_REQUEST, "设备角色不属于所选业务声明")
    if not bindings:
        return
    devices = list(
        await session.scalars(
            select(Device)
            .where(Device.device_code.in_(bindings.values()))
            .order_by(Device.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(devices) != len(bindings) or any(d.work_line_id != workline_id for d in devices):
        raise AppError(ErrorCode.CONFLICT, "角色只能绑定当前工作线已登记的设备")


async def delete_workline(session, workline_id, *, version, actor):
    async with mutation(session):
        row = (await lock_worklines(session, [workline_id]))[0]
        if row.version != version:
            raise AppError(ErrorCode.CONFLICT, "工作线版本已变化")
        if await session.scalar(select(Device.id).where(Device.work_line_id == row.id).limit(1)):
            raise AppError(ErrorCode.CONFLICT, "工作线仍有设备，请先移出或删除设备配置")
        await record_change(
            session,
            actor=actor,
            action="asset:workline:delete",
            resource="work-lines",
            resource_id=row.id,
            before=snapshot(row, (await position_rows(session, [row.id]))[row.id]),
            after={},
        )
        from app.businesses._components.rack_dispatch.models import RackJob

        if await session.scalar(
            select(RackJob.id).where(RackJob.work_line_id == row.id, RackJob.pending).limit(1)
        ):
            raise AppError(ErrorCode.CONFLICT, "工作线仍有未结束货架责任")
        await session.execute(
            delete(WorkLinePosition).where(WorkLinePosition.work_line_id == row.id)
        )
        await session.delete(row)


async def business_configuration_issue(
    session,
    binding: DeviceBinding,
    business_code: str,
    *,
    definition_lookup: Callable[[str], BusinessDefinition],
    required_device_roles: tuple[str, ...] | None = None,
    required_position_slots: tuple[str, ...] | None = None,
) -> str | None:
    """Check declaration completeness in the resolver's locked transaction; grant no permission."""
    # Resolver owns WorkLine → Device locks; never reacquire a workline lock after its device.
    line = await session.get(WorkLine, binding.work_line_id, populate_existing=True)
    if (
        line is None
        or line.version != binding.work_line_version
        or line.device_bindings != binding.device_bindings
        or line.position_bindings != binding.position_bindings
    ):
        raise AppError(ErrorCode.CONFLICT, "工作线绑定已变化，请重试")
    if line.business_code != business_code:
        return "WORKLINE_BUSINESS_MISMATCH"
    try:
        definition = definition_lookup(business_code)
    except LookupError:
        return "WORKLINE_CONFIGURATION_INCOMPLETE"
    roles = {role.role_key for role in definition.device_roles}
    positions = {slot.slot_key for slot in definition.position_slots}
    for selected, declared in (
        (required_device_roles, roles),
        (required_position_slots, positions),
    ):
        if selected is not None and (
            not isinstance(selected, tuple) or any(key not in declared for key in selected)
        ):
            raise ValueError("Operation resources must be declared by the business")
    needed_roles = {r.role_key for r in definition.device_roles if r.required}
    needed_positions = {p.slot_key for p in definition.position_slots if p.required}
    if required_device_roles is not None:
        needed_roles = set(required_device_roles)
    if required_position_slots is not None:
        needed_positions = set(required_position_slots)
    if (
        line.device_bindings.keys() - roles
        or line.position_bindings.keys() - positions
        or any(not line.device_bindings.get(key) for key in needed_roles)
        or any(not line.position_bindings.get(key) for key in needed_positions)
    ):
        return "WORKLINE_CONFIGURATION_INCOMPLETE"
    return None


async def _protect_positions(session, line_id, codes):
    if not codes:
        return
    from app.businesses._components.rack_dispatch.models import RackJob

    if await session.scalar(
        select(RackJob.id)
        .where(RackJob.work_line_id == line_id, RackJob.point.in_(codes), RackJob.pending)
        .limit(1)
    ):
        raise AppError(ErrorCode.CONFLICT, "工作位仍有未结束货架责任")


async def _save_positions(session, line, definition, supplied, *, creating=False):
    declared = (
        {p.slot_key: p.position_type for p in definition.position_slots} if definition else {}
    )
    rack_codes = {
        v for k, v in line.position_bindings.items() if declared.get(k) == "RACK_POSITION"
    }
    handoffs = {
        v for k, v in line.position_bindings.items() if declared.get(k) == "HANDOFF_POSITION"
    }
    if rack_codes & handoffs:
        raise AppError(ErrorCode.INVALID_REQUEST, "货架工作位不能同时是交接口")
    existing = (await position_rows(session, [line.id]))[line.id]
    old = {p.position_code: p for p in existing}
    if supplied is None:
        if not creating:
            if rack_codes - old.keys():
                raise AppError(ErrorCode.CONFLICT, "货架工作位配置不完整")
            return existing
        values = {code: 1 for code in rack_codes}
    else:
        values = {p.position_code: p.capacity for p in supplied}
        if len(values) != len(supplied) or values.keys() - (rack_codes | old.keys()):
            raise AppError(ErrorCode.INVALID_REQUEST, "工作位配置重复或不属于货架工作位")
        if rack_codes - values.keys():
            raise AppError(ErrorCode.CONFLICT, "仍绑定的货架工作位不能删除")
    await _protect_positions(session, line.id, old.keys() - values.keys())
    if values and await session.scalar(
        select(WorkLinePosition.id)
        .where(WorkLinePosition.position_code.in_(values), WorkLinePosition.work_line_id != line.id)
        .limit(1)
    ):
        raise AppError(ErrorCode.CONFLICT, "货架工作位已属于其他工作线")
    for code, row in old.items():
        if code not in values:
            await session.delete(row)
    result = []
    for code, capacity in sorted(values.items()):
        row = old.get(code)
        if row is None:
            row = WorkLinePosition(work_line_id=line.id, position_code=code, capacity=capacity)
            session.add(row)
        else:
            row.capacity = capacity
        result.append(row)
    await session.flush()
    return result
