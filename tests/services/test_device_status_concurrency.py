import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import AppError
from app.integrations.ecs_status import EcsStatusResponse, StatusQueryResult
from app.models import AuditLog, Device, DeviceRoute
from app.schemas.device import DeviceRegisterIn
from app.services.asset_audit import AssetActor
from app.services.device_service import delete_device, lock_device_codes, register_devices
from app.services.device_status_service import DeviceStatusService
from tests.helpers import ecs_status_document


@pytest.mark.parametrize("operation", ["register", "migrate"])
async def test_preview_cannot_expire_while_confirmation_waits_for_database_lock(
    asset_pg, real_redis, wait_for_lock, monkeypatch, operation
):
    from app.core.identifiers import uuid7
    from app.schemas.device import DeviceMigrationIn
    from app.services import device_service

    factory, code = asset_pg
    actor = AssetActor(1, code, None, None)
    origin = "http://127.0.0.1:45821"
    if operation == "migrate":
        async with factory.begin() as session:
            route = DeviceRoute(
                id=str(uuid7()), device_code=code, ecs_origin="http://127.0.0.1:45822"
            )
            session.add(route)
            await session.flush()
            device = Device(device_code=code, display_name=code, current_route_id=route.id)
            session.add(device)
            await session.flush()
            device_id = device.id

    async def query(origin, **kwargs):
        items = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=items.devices,
        )

    preview = await DeviceStatusService(factory, real_redis, query_fn=query).preview(
        actor_id=1, ecs_origin=origin
    )
    expired = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return preview.expires_at if expired else datetime.now(UTC)

    monkeypatch.setattr(device_service, "datetime", Clock)

    async def confirm():
        async with factory() as session:
            if operation == "register":
                return await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=preview.preview_token, device_codes=[code]),
                    actor=actor,
                    redis=real_redis,
                )
            return await device_service.migrate_device_route(
                session,
                device_id,
                DeviceMigrationIn(
                    version=1,
                    expected_origin=route.ecs_origin,
                    preview_token=preview.preview_token,
                    reason="migration",
                    confirmed=True,
                ),
                actor=actor,
                redis=real_redis,
            )

    pending = None
    try:
        async with factory() as blocker:
            await lock_device_codes(blocker, [code])
            pending = asyncio.create_task(confirm())
            await wait_for_lock(pending)
            expired = True
            await blocker.rollback()
        with pytest.raises(AppError) as error:
            await pending
        assert error.value.detail == "PREVIEW_EXPIRED"
        async with factory() as session:
            routes = list(
                await session.scalars(select(DeviceRoute).where(DeviceRoute.device_code == code))
            )
            assert len(routes) == int(operation == "migrate")
            assert (
                await session.scalar(select(AuditLog.id).where(AuditLog.trace_id == code)) is None
            )
    finally:
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await real_redis.delete(
            DeviceStatusService.origin_key(origin), "device-preview:" + preview.preview_token
        )


async def test_concurrent_cross_origin_registration_and_deleted_token_replay(
    asset_pg, real_redis, wait_for_lock
):
    factory, code = asset_pg
    actor = AssetActor(1, code, None, None)

    async def query(origin, **kwargs):
        items = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=items.devices,
        )

    service = DeviceStatusService(factory, real_redis, query_fn=query)
    origins = ["http://127.0.0.1:45681", "http://127.0.0.1:45682"]
    tokens = []
    tasks = []
    try:
        previews = await asyncio.gather(
            *(service.preview(actor_id=1, ecs_origin=o) for o in origins)
        )
        tokens = [p.preview_token for p in previews]

        async def register(token):
            async with factory() as session:
                return await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=token, device_codes=[code]),
                    actor=actor,
                    redis=real_redis,
                )

        async with factory() as blocker:
            await lock_device_codes(blocker, [code])
            tasks = [asyncio.create_task(register(token)) for token in tokens]
            await wait_for_lock(tasks[0])
            await blocker.rollback()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successes = [result for result in results if isinstance(result, list)]
        conflicts = [result for result in results if isinstance(result, AppError)]
        assert len(successes) == len(conflicts) == 1, results
        assert conflicts[0].detail == "PREVIEW_CHANGED"
        current = successes[0][0]
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(DeviceRoute)
                    .where(DeviceRoute.device_code == code)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(AuditLog).where(AuditLog.trace_id == code)
                )
                == 1
            )
            await delete_device(session, current.id, version=current.version, actor=actor)
        for token in tokens:
            with pytest.raises(AppError) as stale:
                await register(token)
            assert stale.value.detail == "PREVIEW_CHANGED"
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        await real_redis.delete(
            *(service.origin_key(o) for o in origins), *("device-preview:" + t for t in tokens)
        )


async def test_registration_audit_failure_rolls_back_route_and_allows_same_preview_retry(
    asset_pg, real_redis, monkeypatch
):
    from app.services import device_service

    factory, code = asset_pg
    origin = "http://127.0.0.1:45683"
    actor = AssetActor(1, code, None, None)

    async def query(origin, **kwargs):
        items = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=items.devices,
        )

    service = DeviceStatusService(factory, real_redis, query_fn=query)
    preview = await service.preview(actor_id=1, ecs_origin=origin)
    data = DeviceRegisterIn(preview_token=preview.preview_token, device_codes=[code])
    original = device_service.record_change

    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    try:
        monkeypatch.setattr(device_service, "record_change", fail)
        async with factory() as session:
            with pytest.raises(RuntimeError):
                await register_devices(session, data, actor=actor, redis=real_redis)
            assert await session.scalar(select(Device.id).where(Device.device_code == code)) is None
            assert (
                await session.scalar(select(DeviceRoute.id).where(DeviceRoute.device_code == code))
                is None
            )
        monkeypatch.setattr(device_service, "record_change", original)
        async with factory() as session:
            assert len(await register_devices(session, data, actor=actor, redis=real_redis)) == 1
    finally:
        await real_redis.delete(
            service.origin_key(origin), "device-preview:" + preview.preview_token
        )


@pytest.mark.parametrize("late_failure", [False, True])
async def test_old_query_is_rejected_after_a_b_a_migration(asset_pg, real_redis, late_failure):
    from app.core.identifiers import uuid7
    from app.integrations.ecs_status import StatusQueryError
    from app.schemas.device import DeviceMigrationIn
    from app.services.device_service import migrate_device_route, read_device

    factory, code = asset_pg
    actor = AssetActor(1, code, None, None)
    a, b = "http://127.0.0.1:45701", "http://127.0.0.1:45702"
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=a)
        session.add(route)
        await session.flush()
        device = Device(device_code=code, display_name=code, current_route_id=route.id)
        session.add(device)
        await session.flush()
        device_id = device.id
    started, release = asyncio.Event(), asyncio.Event()
    first = True

    async def query(origin, **kwargs):
        nonlocal first
        if first:
            first = False
            started.set()
            await release.wait()
            if late_failure:
                raise StatusQueryError(
                    ecs_origin=origin,
                    requested_device_code=code,
                    completed_at=datetime.now(UTC),
                    error_kind="READ_TIMEOUT",
                )
        items = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=kwargs.get("device_code"),
            fetched_at=datetime.now(UTC),
            devices=items.devices,
        )

    service = DeviceStatusService(factory, real_redis, query_fn=query)
    pending = asyncio.create_task(service.refresh_device(device_id))
    tokens = []
    await started.wait()
    try:

        async def migrate(origin, version, expected):
            preview = await service.preview(actor_id=1, ecs_origin=origin)
            tokens.append(preview.preview_token)
            async with factory() as session:
                return await migrate_device_route(
                    session,
                    device_id,
                    DeviceMigrationIn(
                        version=version,
                        expected_origin=expected,
                        preview_token=preview.preview_token,
                        reason="test migration",
                        confirmed=True,
                    ),
                    actor=actor,
                    redis=real_redis,
                )

        middle = await migrate(b, 1, a)
        # Simulate an expired old lease before the newer generation finishes.
        await real_redis.hset(service.origin_key(a), mapping={"lease_until": 0, "next_at": 0})
        latest = await migrate(a, middle.version, b)
        release.set()
        with pytest.raises(AppError) as changed:
            await pending
        assert changed.value.detail == "DEVICE_CONFIG_CHANGED"
        async with factory() as session:
            saved = await read_device(session, device_id)
            assert saved.current_route_id == latest.current_route_id != route.id
            assert saved.version == 3
            assert saved.model_dump() == latest.model_dump()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await real_redis.delete(
            *(service.origin_key(o) for o in [a, b]), *("device-preview:" + t for t in tokens)
        )


async def test_workline_delete_and_assignment_serialize_on_same_line(asset_pg, wait_for_lock):
    from app.core.identifiers import uuid7
    from app.models import WorkLine
    from app.schemas.device import DeviceAssignmentsIn
    from app.services.device_service import assign_devices
    from app.services.workline_service import delete_workline, lock_worklines

    factory, code = asset_pg
    actor = AssetActor(1, code, None, None)
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin="http://127.0.0.1:45711")
        line = WorkLine(line_code=code, line_name="line")
        session.add_all([route, line])
        await session.flush()
        device = Device(device_code=code, display_name=code, current_route_id=route.id)
        session.add(device)
        await session.flush()
        line_id, device_id = line.id, device.id

    async def assign():
        async with factory() as session:
            return await assign_devices(
                session,
                DeviceAssignmentsIn(
                    items=[{"device_id": device_id, "version": 1}], work_line_id=line_id
                ),
                actor=actor,
            )

    async def remove():
        async with factory() as session:
            return await delete_workline(session, line_id, version=1, actor=actor)

    async with factory() as blocker:
        await lock_worklines(blocker, [line_id])
        tasks = [asyncio.create_task(assign()), asyncio.create_task(remove())]
        await wait_for_lock(tasks[0])
        await blocker.rollback()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, AppError) for result in results) == 1, results
    async with factory() as session:
        current = await session.get(Device, device_id)
        remaining = await session.get(WorkLine, line_id)
        assert (remaining is not None and current.work_line_id == line_id) or (
            remaining is None and current.work_line_id is None
        )


async def test_registration_expiring_during_audit_rolls_back_every_write(
    asset_pg, real_redis, monkeypatch
):
    from app.services import device_service

    factory, code = asset_pg
    actor = AssetActor(1, code, None, None)
    origin = "http://127.0.0.1:45823"

    async def query(origin, **kwargs):
        items = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=items.devices,
        )

    preview = await DeviceStatusService(factory, real_redis, query_fn=query).preview(
        actor_id=1, ecs_origin=origin
    )
    original = device_service.record_change
    expired = False

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return preview.expires_at if expired else datetime.now(UTC)

    async def delayed_audit(*args, **kwargs):
        nonlocal expired
        await original(*args, **kwargs)
        expired = True

    monkeypatch.setattr(device_service, "datetime", Clock)
    monkeypatch.setattr(device_service, "record_change", delayed_audit)
    try:
        async with factory() as session:
            with pytest.raises(AppError) as error:
                await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=preview.preview_token, device_codes=[code]),
                    actor=actor,
                    redis=real_redis,
                )
            assert error.value.detail == "PREVIEW_EXPIRED"
            assert await session.scalar(select(Device.id).where(Device.device_code == code)) is None
            assert (
                await session.scalar(select(DeviceRoute.id).where(DeviceRoute.device_code == code))
                is None
            )
            assert (
                await session.scalar(select(AuditLog.id).where(AuditLog.trace_id == code)) is None
            )
    finally:
        await real_redis.delete(
            DeviceStatusService.origin_key(origin), "device-preview:" + preview.preview_token
        )


@pytest.mark.parametrize("change", ["version", "newer_query", "lost_key"])
@pytest.mark.parametrize("late_failure", [False, True])
async def test_inflight_result_is_rejected_when_configuration_or_generation_changes(
    asset_pg, real_redis, change, late_failure
):
    from sqlalchemy import update

    from app.core.identifiers import uuid7
    from app.integrations.ecs_status import StatusQueryError

    factory, code = asset_pg
    origin = "http://127.0.0.1:45903"
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
        session.add(route)
        await session.flush()
        device = Device(device_code=code, display_name=code, current_route_id=route.id)
        session.add(device)
        await session.flush()
        device_id = device.id
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def query(origin, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            if late_failure:
                raise StatusQueryError(
                    ecs_origin=origin,
                    requested_device_code=code,
                    completed_at=datetime.now(UTC),
                    error_kind="READ_TIMEOUT",
                )
        parsed = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=code,
            fetched_at=datetime.now(UTC),
            devices=parsed.devices,
        )

    service = DeviceStatusService(factory, real_redis, query_fn=query)
    pending = asyncio.create_task(service.refresh_device(device_id))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            if change == "version":
                # This commit also proves no database row lock is held across ECS HTTP.
                async with factory.begin() as session:
                    await session.execute(
                        update(Device)
                        .where(Device.id == device_id)
                        .values(display_name="edited", version=2)
                    )
            elif change == "newer_query":
                await real_redis.hset(
                    service.origin_key(origin), mapping={"lease_until": 0, "next_at": 0}
                )
                latest = await service.refresh_device(device_id)
                assert latest.device_id == device_id
            else:
                await real_redis.delete(service.origin_key(origin))
            release.set()
            with pytest.raises(AppError) as rejected:
                await pending
            assert rejected.value.detail == (
                "DEVICE_CONFIG_CHANGED" if change == "version" else "QUERY_SUPERSEDED"
            )
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await real_redis.delete(service.origin_key(origin))


@pytest.mark.parametrize("change", ["delete", "migrate"])
async def test_device_list_preserves_selected_configuration_during_concurrent_change(
    asset_pg, monkeypatch, change
):
    from sqlalchemy import update

    from app.core.identifiers import uuid7
    from app.schemas.query import FilterGroup, UserListParams
    from app.services import pagination
    from app.services.device_service import list_devices

    factory, code = asset_pg
    first, second = "http://127.0.0.1:45911", "http://127.0.0.1:45912"
    async with factory.begin() as session:
        routes = [
            DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
            for origin in [first, second]
        ]
        session.add_all(routes)
        await session.flush()
        device = Device(device_code=code, display_name=code, current_route_id=routes[0].id)
        session.add(device)
        await session.flush()
        device_id, old_route, new_route = device.id, routes[0].id, routes[1].id

    paginate = pagination.paginate

    async def change_after_selection(*args, **kwargs):
        page = await paginate(*args, **kwargs)
        async with factory() as writer:
            if change == "delete":
                await delete_device(
                    writer, device_id, version=1, actor=AssetActor(1, code, None, None)
                )
            else:
                await writer.execute(
                    update(Device)
                    .where(Device.id == device_id)
                    .values(current_route_id=new_route, version=2)
                )
                await writer.commit()
        return page

    monkeypatch.setattr(pagination, "paginate", change_after_selection)
    filters = FilterGroup.model_validate(
        {
            "conditions": [
                {"field": "device_code", "op": "eq", "value": code},
                {"field": "ecs_origin", "op": "eq", "value": first},
            ]
        }
    )
    async with factory() as reader:
        page = await list_devices(reader, UserListParams(), filters)
    assert page["total"] == len(page["items"]) == 1
    result = page["items"][0]
    assert (result.id, result.ecs_origin, result.current_route_id, result.version) == (
        device_id,
        first,
        old_route,
        1,
    )
    async with factory() as reader:
        current = await reader.get(Device, device_id)
        if change == "delete":
            assert current is None
        else:
            assert current.current_route_id == new_route and current.version == 2
        assert await reader.get(DeviceRoute, old_route) is not None
