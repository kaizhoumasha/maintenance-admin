import pytest

from app.core.errors import AppError
from app.core.identifiers import uuid7
from app.models import Device, DeviceRoute
from app.schemas.device import DeviceAssignmentsIn
from app.schemas.workline import WorkLineCreate, WorkLineUpdate
from app.services import device_service, workline_service
from app.services.asset_audit import AssetActor
from tests.helpers import workline_definition_lookup

ACTOR = AssetActor(1, "role-bindings", None, None)


async def line_device(session, code="SCAN13", line_code="L4"):
    line = await workline_service.create_workline(
        session,
        WorkLineCreate(
            line_code=line_code,
            line_name=line_code,
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin="http://127.0.0.1:8080")
    session.add(route)
    await session.flush()
    device = Device(
        device_code=code, display_name=code, current_route_id=route.id, work_line_id=line.id
    )
    session.add(device)
    await session.commit()
    return line, device


async def bind(session, line, bindings, positions=None):
    return await workline_service.update_workline(
        session,
        line.id,
        WorkLineUpdate(
            line_name=line.line_name,
            version=line.version,
            device_bindings=bindings,
            position_bindings=positions or {},
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )


async def test_role_and_positions_are_resolved_from_workline_bindings(db):
    line, device = await line_device(db)
    updated = await bind(db, line, {"SCAN1": "SCAN13"}, {"INLET": "CNV0401"})
    assert updated.device_bindings == {"SCAN1": "SCAN13"}
    from app.services.device_bindings import resolve_binding, resolve_role

    binding = await resolve_binding(db, device.device_code)
    assert binding.device_id == device.id
    assert binding.device_role == "SCAN1"
    assert binding.work_line_id == line.id
    assert binding.position_bindings == {"INLET": "CNV0401"}
    assert (await resolve_role(db, line.id, "SCAN1")) == binding
    assert await resolve_binding(db, "MISSING") is None


async def test_bound_device_requires_unbinding_before_removal(db):
    line, device = await line_device(db)
    line = await bind(db, line, {"SCAN1": device.device_code})
    device_id = device.id
    for operation in ("assign", "delete"):
        with pytest.raises(AppError) as error:
            if operation == "assign":
                await device_service.assign_devices(
                    db,
                    DeviceAssignmentsIn(
                        items=[{"device_id": device_id, "version": 1}], work_line_id=None
                    ),
                    actor=ACTOR,
                )
            else:
                await device_service.delete_device(db, device_id, version=1, actor=ACTOR)
        assert error.value.code == "CONFLICT"
    await bind(db, line, {})
    await device_service.delete_device(db, device_id, version=1, actor=ACTOR)


async def test_device_cannot_be_bound_to_another_workline(db):
    line, device = await line_device(db)
    other = await workline_service.create_workline(
        db,
        WorkLineCreate(
            line_code="OTHER",
            line_name="其他",
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    with pytest.raises(AppError) as error:
        await bind(db, other, {"SCAN1": device.device_code})
    assert error.value.code == "CONFLICT"
    assert (await workline_service.read_workline(db, line.id)).device_bindings == {}


async def test_device_has_only_one_logical_role(db):
    line, device = await line_device(db)
    with pytest.raises(ValueError):
        WorkLineUpdate(
            line_name=line.line_name,
            version=line.version,
            device_bindings={"SCAN1": device.device_code, "SCAN2": device.device_code},
        )


async def test_cached_membership_cannot_bypass_a_new_role_binding(db):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import WorkLine

    first, device = await line_device(db)
    second = await workline_service.create_workline(
        db,
        WorkLineCreate(
            line_code="SECOND",
            line_name="Second",
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    third = await workline_service.create_workline(
        db,
        WorkLineCreate(
            line_code="THIRD",
            line_name="Third",
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    cached_second = await db.get(WorkLine, second.id)
    assert cached_second.device_bindings == {}
    device_id, second_id, third_id = device.id, second.id, third.id
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as writer:
        current = await writer.get(Device, device_id)
        current.work_line_id = second_id
        current.version += 1
        line = await writer.get(WorkLine, second_id)
        line.device_bindings = {"SCAN1": current.device_code}
        line.version += 1
        await writer.commit()
    with pytest.raises(AppError) as conflict:
        await device_service.assign_devices(
            db,
            DeviceAssignmentsIn(
                items=[{"device_id": device_id, "version": 2}],
                work_line_id=third_id,
            ),
            actor=ACTOR,
        )
    assert conflict.value.code == "CONFLICT"
    await db.refresh(device)
    assert device.work_line_id == second_id


async def test_deletion_rechecks_membership_after_workline_lock_wait(db, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import WorkLine

    first, device = await line_device(db)
    second = await workline_service.create_workline(
        db,
        WorkLineCreate(
            line_code="SECOND",
            line_name="Second",
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    cached = await db.get(WorkLine, second.id)
    assert cached.device_bindings == {}
    device_id, second_id = device.id, second.id
    lock = device_service.lock_worklines

    async def move_before_lock(session, ids):
        async with AsyncSession(bind=db.bind, expire_on_commit=False) as writer:
            current = await writer.get(Device, device_id)
            current.work_line_id = second_id
            current.version += 1
            line = await writer.get(WorkLine, second_id)
            line.device_bindings = {"SCAN1": current.device_code}
            line.version += 1
            await writer.commit()
        return await lock(session, ids)

    monkeypatch.setattr(device_service, "lock_worklines", move_before_lock)
    with pytest.raises(AppError) as conflict:
        await device_service.delete_device(db, device_id, version=2, actor=ACTOR)
    assert conflict.value.code == "CONFLICT"


async def test_worklines_reuse_role_handler_with_their_own_positions(db, monkeypatch):
    import json

    from app.integrations.ecs_callback import CallbackPayload, registry
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.services.device_bindings import resolve_binding

    monkeypatch.setattr(registry, "_registry", {})
    observed = []

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def scan(ctx, payload):
        observed.append((ctx.device_code, ctx.work_line_id, ctx.binding.position_bindings["INLET"]))

    groups = []
    for code, line_code, position in [
        ("STATION_SCAN1", "L1", "CNV0101"),
        ("STATION_SCAN13", "L4", "CNV0401"),
    ]:
        line, device = await line_device(db, code, line_code)
        await bind(db, line, {"SCAN1": code}, {"INLET": position})
        groups.append((code, line.id, position))
    await db.commit()
    for code, _, _ in groups:
        async with db.begin():
            answer = await receive_ecs_event(
                db,
                json.dumps(
                    {
                        "device_code": code,
                        "event_type": "SCAN_COMPLETED",
                        "timestamp": 1,
                    }
                ).encode(),
                resolve_binding=resolve_binding,
            )
            assert answer.status_code == 200
    assert observed == groups
