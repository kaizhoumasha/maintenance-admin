import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app.core.identifiers import uuid7
from app.models import Device, DeviceRoute, WorkLine


async def test_delete_only_current_configuration_and_ids_are_not_reused(db):
    await db.execute(text("PRAGMA foreign_keys=ON"))
    route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1:8001")
    line = WorkLine(line_code="L1", line_name="线1")
    db.add_all([route, line])
    await db.flush()
    device = Device(
        device_code="D1", display_name="设备", current_route_id=route.id, work_line_id=line.id
    )
    db.add(device)
    await db.flush()
    original_id = device.id
    await db.commit()
    await db.execute(delete(Device).where(Device.id == original_id))
    await db.commit()
    assert await db.get(DeviceRoute, route.id) is not None
    new_route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1:8002")
    db.add(new_route)
    await db.flush()
    replacement = Device(device_code="D1", display_name="设备", current_route_id=new_route.id)
    db.add(replacement)
    await db.commit()
    assert replacement.id != original_id
    assert new_route.id != route.id
    assert replacement.work_line_id is None


async def test_current_device_code_is_globally_unique(db):
    for origin in ("http://127.0.0.1:8001", "http://127.0.0.1:8002"):
        route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin=origin)
        db.add(route)
        await db.flush()
        db.add(Device(device_code="D1", display_name="设备", current_route_id=route.id))
        if origin.endswith("8001"):
            await db.commit()
        else:
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
