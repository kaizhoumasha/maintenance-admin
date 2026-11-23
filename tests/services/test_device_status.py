import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import AppError
from app.core.identifiers import uuid7
from app.integrations.ecs_status import EcsStatusResponse, StatusQueryResult
from app.models import Device, DeviceRoute
from app.services.device_status_service import DeviceStatusService
from tests.helpers import ecs_status_document


async def create_device(factory, code="D1", origin=None):
    origin = origin or f"http://127.0.0.1:{10000 + int(uuid7().hex[-4:], 16) % 50000}"
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin=origin)
        session.add(route)
        await session.flush()
        device = Device(device_code=code, display_name=code, current_route_id=route.id)
        session.add(device)
        await session.flush()
        return device.id, origin


def response(origin, *codes):
    import json

    parsed = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(*codes)))
    return StatusQueryResult(
        ecs_origin=origin,
        requested_device_code=None,
        fetched_at=datetime.now(UTC),
        devices=parsed.devices,
    )


async def test_refresh_returns_query_source_without_database_writes(asset_factory, real_redis):
    from sqlalchemy import event

    async with asset_factory() as session:
        engine = session.get_bind()
    writes = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    device_id, origin = await create_device(asset_factory)

    async def query(origin, **kwargs):
        return response(origin, "D1")

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    try:
        event.listen(engine, "before_cursor_execute", observe)
        refreshed = await service.refresh_device(device_id)
        assert refreshed.result.devices[0].state.status == "OFFLINE"
        assert refreshed.device_id == device_id and refreshed.device_version == 1
        assert refreshed.result.ecs_origin == origin
        assert writes == []
        with pytest.raises(AppError) as cooled:
            await service.refresh_device(device_id)
        assert cooled.value.code == "TOO_MANY_REQUESTS"
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        await real_redis.delete(service.origin_key(origin))


async def test_late_query_never_updates_same_code_reregistration(asset_factory, real_redis):
    from app.services.asset_audit import AssetActor
    from app.services.device_service import delete_device, read_device

    old_id, origin = await create_device(asset_factory)
    started, release = asyncio.Event(), asyncio.Event()

    async def query(origin, **kwargs):
        started.set()
        await release.wait()
        return response(origin, "D1")

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    task = asyncio.create_task(service.refresh_device(old_id))
    await started.wait()
    try:
        async with asset_factory() as session:
            await delete_device(session, old_id, version=1, actor=AssetActor(1, "test", None, None))
        new_id, _ = await create_device(asset_factory, origin=origin)
        release.set()
        with pytest.raises(AppError) as missing:
            await task
        assert missing.value.code == "NOT_FOUND"
        async with asset_factory() as session:
            new = await read_device(session, new_id)
        assert new.id == new_id != old_id
        assert new.version == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await real_redis.delete(service.origin_key(origin))


@pytest.mark.parametrize("failure", ["empty", "not_found", "timeout", "limited"])
async def test_each_query_returns_only_its_current_result(asset_factory, real_redis, failure):
    from app.integrations.ecs_status import StatusQueryError

    device_id, origin = await create_device(asset_factory)
    mode = "success"

    async def query(origin, **kwargs):
        if mode == "success":
            return response(origin, "D1")
        if mode == "empty":
            return StatusQueryResult(
                ecs_origin=origin,
                requested_device_code="D1",
                fetched_at=datetime.now(UTC),
                devices=(),
            )
        raise StatusQueryError(
            ecs_origin=origin,
            requested_device_code="D1",
            completed_at=datetime.now(UTC),
            error_kind={
                "not_found": "NOT_FOUND",
                "timeout": "READ_TIMEOUT",
                "limited": "HTTP_ERROR",
            }[mode],
            http_status=404 if mode == "not_found" else 429 if mode == "limited" else None,
            retry_after_at=datetime.now(UTC) + timedelta(seconds=3600)
            if mode == "limited"
            else None,
        )

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    try:
        first = await service.refresh_device(device_id)
        mode = failure
        await real_redis.hset(service.origin_key(origin), "next_at", 0)
        if failure == "empty":
            latest = await service.refresh_device(device_id)
            assert latest.result.devices == ()
        else:
            with pytest.raises(StatusQueryError):
                await service.refresh_device(device_id)
        from app.services.device_service import read_device

        async with asset_factory() as session:
            config = await read_device(session, device_id)
        assert config.version == first.device_version
        assert "observation" not in config.model_dump()
        if failure == "limited":
            with pytest.raises(AppError) as limited:
                await service.refresh_device(device_id)
            assert int(limited.value.headers["Retry-After"]) >= 3599
    finally:
        await real_redis.delete(service.origin_key(origin))


async def test_origin_lease_blocks_competing_query_and_cancel_releases_it(
    asset_factory, real_redis
):
    device_id, origin = await create_device(asset_factory)
    started = asyncio.Event()

    async def query(origin, **kwargs):
        started.set()
        await asyncio.Event().wait()

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    task = asyncio.create_task(service.refresh_device(device_id))
    try:
        await started.wait()
        with pytest.raises(AppError) as busy:
            await service.fetch(origin)
        assert busy.value.detail == "QUERY_IN_PROGRESS"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert int(await real_redis.hget(service.origin_key(origin), "lease_until")) == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await real_redis.delete(service.origin_key(origin))


async def test_coordination_failure_does_not_query_or_prevent_configuration_deletion(asset_factory):
    from redis.exceptions import ConnectionError

    from app.services.asset_audit import AssetActor
    from app.services.device_service import delete_device

    device_id, origin = await create_device(asset_factory)

    class BrokenRedis:
        async def eval(self, *args):
            raise ConnectionError("private connection failure")

    async def forbidden(*args, **kwargs):
        raise AssertionError("cache unavailable must not query ECS")

    service = DeviceStatusService(asset_factory, BrokenRedis(), query_fn=forbidden)
    with pytest.raises(AppError) as unavailable:
        await service.refresh_device(device_id)
    assert unavailable.value.code == "DATABASE_UNAVAILABLE"
    async with asset_factory() as session:
        await delete_device(
            session, device_id, version=1, actor=AssetActor(1, "cache-failure", None, None)
        )
        assert await session.get(Device, device_id) is None


async def test_lost_origin_key_does_not_require_stored_observations(asset_factory, real_redis):
    device_id, origin = await create_device(asset_factory)
    calls = []

    async def query(origin, **kwargs):
        calls.append(origin)
        return response(origin, "D1")

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    try:
        await service.refresh_device(device_id)
        await real_redis.delete(service.origin_key(origin))
        result = await service.refresh_device(device_id)
        assert result.result.devices[0].device.device_code == "D1"
        assert calls == [origin, origin]
    finally:
        await real_redis.delete(service.origin_key(origin))


async def test_late_rate_limit_preserves_new_owner_and_extends_origin_wait(
    asset_factory, real_redis
):
    from app.integrations.ecs_status import StatusQueryError

    _, origin = await create_device(asset_factory)
    started, release = asyncio.Event(), asyncio.Event()
    newer_started, newer_release = asyncio.Event(), asyncio.Event()
    calls = 0
    wait_until = datetime.now(UTC) + timedelta(seconds=900)

    async def query(origin, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            raise StatusQueryError(
                ecs_origin=origin,
                requested_device_code=None,
                completed_at=datetime.now(UTC),
                error_kind="HTTP_ERROR",
                http_status=429,
                retry_after_at=wait_until,
            )
        newer_started.set()
        await newer_release.wait()
        return response(origin, "D1")

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    pending = asyncio.create_task(service.fetch(origin))
    newer = None
    try:
        await started.wait()
        await real_redis.hset(service.origin_key(origin), mapping={"lease_until": 0, "next_at": 0})
        newer = asyncio.create_task(service.fetch(origin))
        await newer_started.wait()
        owner = await real_redis.hget(service.origin_key(origin), "token")
        lease = await real_redis.hget(service.origin_key(origin), "lease_until")
        release.set()
        with pytest.raises(AppError) as superseded:
            await pending
        assert superseded.value.detail == "QUERY_SUPERSEDED"
        assert await real_redis.hget(service.origin_key(origin), "token") == owner
        assert await real_redis.hget(service.origin_key(origin), "lease_until") == lease
        newer_release.set()
        await newer
        with pytest.raises(AppError) as limited:
            await service.fetch(origin)
        assert int(limited.value.headers["Retry-After"]) >= 899
    finally:
        release.set()
        newer_release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if newer:
            await asyncio.gather(newer, return_exceptions=True)
        await real_redis.delete(service.origin_key(origin))
