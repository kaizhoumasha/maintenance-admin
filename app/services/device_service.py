import asyncio
import hashlib
from datetime import UTC, datetime

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import func, select, text

from app.core.errors import AppError, ErrorCode
from app.core.identifiers import uuid7
from app.models import Device, DeviceRoute, WorkLine
from app.schemas.device import DeviceRead, PreviewSnapshot
from app.services.asset_audit import mutation, record_change
from app.services.workline_service import lock_worklines


async def cache_call(operation):
    try:
        async with asyncio.timeout(5):
            return await operation
    except (TimeoutError, RedisError):
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "设备查询协调缓存暂不可用") from None


async def list_devices(session, params, filters=None):
    from app.services.filtering import FieldSpec, apply_filter
    from app.services.pagination import paginate

    stmt = select(Device).join(DeviceRoute, Device.current_route_id == DeviceRoute.id)
    stmt = (
        apply_filter(
            stmt,
            filters,
            {
                "device_code": FieldSpec(Device.device_code, str, ("eq", "in", "ilike")),
                "display_name": FieldSpec(Device.display_name, str, ("eq", "ilike")),
                "work_line_id": FieldSpec(
                    Device.work_line_id, int, ("eq", "in", "is_null", "not_null")
                ),
                "ecs_origin": FieldSpec(DeviceRoute.ecs_origin, str, ("eq", "in")),
            },
        )
        .order_by(Device.sort_order, Device.id)
        .execution_options(populate_existing=True)
    )
    page = await paginate(session, stmt, params)
    if not page["items"]:
        return page
    # Keep the selected configuration even if it changes before serialization.
    routes = {
        route.id: route
        for route in await session.scalars(
            select(DeviceRoute).where(
                DeviceRoute.id.in_([row.current_route_id for row in page["items"]])
            )
        )
    }
    page["items"] = [_device_read(row, routes.get(row.current_route_id)) for row in page["items"]]
    return page


async def get_device(session, device_id, *, lock=False):
    stmt = select(Device).where(Device.id == device_id).execution_options(populate_existing=True)
    row = await session.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "设备配置不存在")
    return row


async def read_device(session, device_id):
    row = await get_device(session, device_id)
    route = await session.get(DeviceRoute, row.current_route_id)
    return _device_read(row, route)


def _device_read(row, route):
    if route is None or route.device_code != row.device_code:
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "设备路由配置不一致")
    fields = {column.key: getattr(row, column.key) for column in Device.__table__.columns}
    return DeviceRead(**fields, ecs_origin=route.ecs_origin)


async def snapshot(session, row):
    fields = {
        key: getattr(row, key)
        for key in (
            "id",
            "device_code",
            "display_name",
            "work_line_id",
            "description",
            "sort_order",
            "version",
            "current_route_id",
        )
    }
    route = await session.get(DeviceRoute, row.current_route_id)
    line = await session.get(WorkLine, row.work_line_id) if row.work_line_id else None
    return {
        **fields,
        "ecs_origin": route.ecs_origin if route else None,
        "work_line_code": line.line_code if line else None,
    }


async def update_device(session, device_id, data, *, actor):
    async with mutation(session):
        row = await get_device(session, device_id, lock=True)
        if row.version != data.version:
            raise AppError(ErrorCode.CONFLICT, "设备配置版本已变化")
        before = await snapshot(session, row)
        for key, value in data.model_dump(exclude={"version"}).items():
            setattr(row, key, value)
        row.version += 1
        await record_change(
            session,
            actor=actor,
            action="asset:device:update",
            resource="devices",
            resource_id=row.id,
            before=before,
            after=await snapshot(session, row),
        )
    return await read_device(session, device_id)


async def assign_devices(session, data, *, actor):
    versions = {item.device_id: item.version for item in data.items}
    async with mutation(session):
        original = list(
            await session.scalars(
                select(Device)
                .where(Device.id.in_(versions))
                .execution_options(populate_existing=True)
            )
        )
        memberships = {d.id: d.work_line_id for d in original}
        line_ids = {d.work_line_id for d in original if d.work_line_id is not None}
        if data.work_line_id is not None:
            line_ids.add(data.work_line_id)
        await lock_worklines(session, line_ids)
        rows = list(
            await session.scalars(
                select(Device)
                .where(Device.id.in_(versions))
                .order_by(Device.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if {d.id for d in rows} != set(versions):
            raise AppError(ErrorCode.NOT_FOUND, "设备配置不存在")
        for row in rows:
            if row.work_line_id != memberships[row.id]:
                raise AppError(ErrorCode.CONFLICT, "设备归属已变化，请重试")
            if row.version != versions[row.id]:
                raise AppError(ErrorCode.CONFLICT, "设备配置版本已变化")
            if row.work_line_id != data.work_line_id:
                await require_unbound(session, row)
            before = await snapshot(session, row)
            row.work_line_id = data.work_line_id
            row.version += 1
            await record_change(
                session,
                actor=actor,
                action="asset:device:assign",
                resource="devices",
                resource_id=row.id,
                before=before,
                after=await snapshot(session, row),
            )
    return [await read_device(session, device_id) for device_id in versions]


async def delete_device(session, device_id, *, version, actor):
    async with mutation(session):
        original = await get_device(session, device_id)
        original_line_id = original.work_line_id
        if original.work_line_id is not None:
            await lock_worklines(session, [original.work_line_id])
        row = await get_device(session, device_id, lock=True)
        if row.work_line_id != original_line_id:
            raise AppError(ErrorCode.CONFLICT, "设备归属已变化，请重试")
        if row.version != version:
            raise AppError(ErrorCode.CONFLICT, "设备配置版本已变化")
        await require_unbound(session, row)
        await record_change(
            session,
            actor=actor,
            action="asset:device:delete",
            resource="devices",
            resource_id=row.id,
            before=await snapshot(session, row),
            after={},
        )
        await session.delete(row)


async def require_unbound(session, device):
    line = await session.get(WorkLine, device.work_line_id) if device.work_line_id else None
    if line and device.device_code in line.device_bindings.values():
        raise AppError(ErrorCode.CONFLICT, "设备已绑定工作线角色，请先解绑")


async def lock_device_codes(session, codes):
    if session.get_bind().dialect.name == "postgresql":
        for code in sorted(set(codes)):
            key = int.from_bytes(
                hashlib.sha256(("device-config:" + code).encode()).digest()[:8], signed=True
            )
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def load_preview(redis, token, actor):
    raw = await cache_call(redis.get("device-preview:" + token))
    if raw is None:
        raise AppError(ErrorCode.NOT_FOUND, "预览不存在或已失效")
    try:
        snapshot = PreviewSnapshot.model_validate_json(raw)
    except ValidationError:
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "预览缓存无效，请重新获取") from None
    if snapshot.actor_id != actor.user_id:
        raise AppError(ErrorCode.NOT_FOUND, "预览不存在或已失效")
    require_live_preview(snapshot)
    return snapshot


def require_live_preview(preview):
    if preview.expires_at <= datetime.now(UTC):
        raise AppError(ErrorCode.CONFLICT, "预览已过期", detail="PREVIEW_EXPIRED")


async def register_devices(session, data, *, actor, redis):
    preview = await load_preview(redis, data.preview_token, actor)
    codes = data.device_codes
    ids = []
    async with mutation(session):
        await lock_device_codes(session, codes)
        if data.work_line_id is not None:
            await lock_worklines(session, [data.work_line_id])
        require_live_preview(preview)
        for code in codes:
            entry = preview.entries.get(code)
            if entry is None or entry.classification != "NEW":
                raise AppError(
                    ErrorCode.CONFLICT, "请选择新预览中的未登记设备", detail="PREVIEW_CHANGED"
                )
            count = await session.scalar(
                select(func.count()).select_from(DeviceRoute).where(DeviceRoute.device_code == code)
            )
            if count != entry.route_history_count or await session.scalar(
                select(Device.id).where(Device.device_code == code)
            ):
                raise AppError(
                    ErrorCode.CONFLICT, "设备登记已变化，请重新预览", detail="PREVIEW_CHANGED"
                )
            route = DeviceRoute(
                id=str(uuid7()),
                device_code=code,
                ecs_origin=preview.ecs_origin,
                created_by=actor.user_id,
            )
            session.add(route)
            await session.flush()
            device = Device(
                device_code=code,
                display_name=entry.data.device.device_name or code,
                work_line_id=data.work_line_id,
                current_route_id=route.id,
            )
            session.add(device)
            await session.flush()
            await record_change(
                session,
                actor=actor,
                action="asset:device:register",
                resource="devices",
                resource_id=device.id,
                before={},
                after=await snapshot(session, device),
            )
            ids.append(device.id)
        require_live_preview(preview)
    return [await read_device(session, device_id) for device_id in ids]


async def migrate_device_route(session, device_id, data, *, actor, redis):
    from app.integrations.ecs_status import normalize_status_origin

    preview = await load_preview(redis, data.preview_token, actor)
    async with mutation(session):
        initial = await get_device(session, device_id)
        await lock_device_codes(session, [initial.device_code])
        if initial.work_line_id is not None:
            await lock_worklines(session, [initial.work_line_id])
        row = await get_device(session, device_id, lock=True)
        require_live_preview(preview)
        entry = preview.entries.get(row.device_code)
        origin = (await session.get(DeviceRoute, row.current_route_id)).ecs_origin
        if (
            entry is None
            or entry.classification != "ADDRESS_CONFLICT"
            or entry.device_id != row.id
            or entry.version != row.version
            or entry.route_id != row.current_route_id
            or data.version != row.version
            or normalize_status_origin(data.expected_origin) != origin
            or preview.ecs_origin == origin
        ):
            raise AppError(
                ErrorCode.CONFLICT, "设备路由已变化，请重新预览", detail="PREVIEW_CHANGED"
            )
        before = await snapshot(session, row)
        route = DeviceRoute(
            id=str(uuid7()),
            device_code=row.device_code,
            ecs_origin=preview.ecs_origin,
            created_by=actor.user_id,
        )
        session.add(route)
        await session.flush()
        row.current_route_id = route.id
        row.version += 1
        await record_change(
            session,
            actor=actor,
            action="asset:device:migrate",
            resource="devices",
            resource_id=row.id,
            before=before,
            after=await snapshot(session, row),
            reason=data.reason.strip(),
        )
        require_live_preview(preview)
    return await read_device(session, device_id)
