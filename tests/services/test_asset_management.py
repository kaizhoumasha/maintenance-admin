import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.core.identifiers import uuid7
from app.models import AuditLog, Device, DeviceRoute
from app.schemas.device import DeviceAssignmentsIn
from app.schemas.workline import WorkLineCreate
from app.services import device_service, workline_service
from app.services.asset_audit import AssetActor
from tests.helpers import workline_definition_lookup

ACTOR = AssetActor(1, "asset-test", None, None)


async def test_delete_configuration_preserves_route_and_transaction_audit(db):
    route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1")
    db.add(route)
    await db.flush()
    device = Device(device_code="D1", display_name="设备", current_route_id=route.id)
    db.add(device)
    await db.commit()
    await device_service.delete_device(db, device.id, version=1, actor=ACTOR)
    assert await db.get(Device, device.id) is None
    assert await db.get(DeviceRoute, route.id) is not None
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "asset:device:delete"))
    assert log.detail["before"]["device_code"] == "D1"
    assert log.detail["after"] == {}
    with pytest.raises(AppError) as missing:
        await device_service.delete_device(db, device.id, version=1, actor=ACTOR)
    assert missing.value.code == "NOT_FOUND"


async def test_assignments_are_atomic_and_nonempty_workline_cannot_be_deleted(db):
    line = await workline_service.create_workline(
        db,
        WorkLineCreate(
            line_code="L1",
            line_name="一线",
            business_code="test_sorting",
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1")
    db.add(route)
    await db.flush()
    device = Device(device_code="D1", display_name="设备", current_route_id=route.id)
    db.add(device)
    await db.commit()
    device_id = device.id
    with pytest.raises(AppError):
        await device_service.assign_devices(
            db,
            DeviceAssignmentsIn(
                items=[{"device_id": device.id, "version": 1}, {"device_id": 999, "version": 1}],
                work_line_id=line.id,
            ),
            actor=ACTOR,
        )
    await db.refresh(device)
    assert device.work_line_id is None
    await device_service.assign_devices(
        db,
        DeviceAssignmentsIn(items=[{"device_id": device.id, "version": 1}], work_line_id=line.id),
        actor=ACTOR,
    )
    with pytest.raises(AppError):
        await workline_service.delete_workline(db, line.id, version=1, actor=ACTOR)
    await device_service.delete_device(db, device_id, version=2, actor=ACTOR)
    await workline_service.delete_workline(db, line.id, version=1, actor=ACTOR)


async def test_audit_failure_rolls_back_deletion(db, monkeypatch):
    route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1")
    db.add(route)
    await db.flush()
    device = Device(device_code="D1", display_name="设备", current_route_id=route.id)
    db.add(device)
    await db.commit()
    device_id = device.id

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(device_service, "record_change", fail)
    with pytest.raises(RuntimeError):
        await device_service.delete_device(db, device_id, version=1, actor=ACTOR)
    assert await db.get(Device, device_id) is not None
