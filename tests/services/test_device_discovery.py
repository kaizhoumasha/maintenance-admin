import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import AppError
from app.core.identifiers import uuid7
from app.integrations.ecs_status import EcsStatusResponse, StatusQueryResult
from app.models import DeviceRoute
from app.schemas.device import DeviceRegisterIn
from app.services.asset_audit import AssetActor
from app.services.device_service import delete_device, register_devices
from app.services.device_status_service import DeviceStatusService
from tests.helpers import ecs_status_document


async def test_deleted_device_can_be_reregistered_but_old_previews_cannot_replay(
    asset_factory, real_redis
):
    code = "T" + uuid7().hex
    origin = "http://127.0.0.1:45678"
    actor = AssetActor(1, "discovery-test", None, None)

    async def query(origin, **kwargs):
        parsed = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document(code)))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=parsed.devices,
        )

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    tokens = []
    try:
        first = await service.preview(actor_id=actor.user_id, ecs_origin=origin)
        tokens.append(first.preview_token)
        await real_redis.hset(service.origin_key(origin), "next_at", 0)
        earlier = await service.preview(actor_id=actor.user_id, ecs_origin=origin)
        tokens.append(earlier.preview_token)
        async with asset_factory() as session:
            old = (
                await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=first.preview_token, device_codes=[code]),
                    actor=actor,
                    redis=real_redis,
                )
            )[0]
            await delete_device(session, old.id, version=old.version, actor=actor)
            for token in tokens:
                with pytest.raises(AppError) as stale:
                    await register_devices(
                        session,
                        DeviceRegisterIn(preview_token=token, device_codes=[code]),
                        actor=actor,
                        redis=real_redis,
                    )
                assert stale.value.detail == "PREVIEW_CHANGED"
        await real_redis.hset(service.origin_key(origin), "next_at", 0)
        fresh = await service.preview(actor_id=actor.user_id, ecs_origin=origin)
        tokens.append(fresh.preview_token)
        async with asset_factory() as session:
            new = (
                await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=fresh.preview_token, device_codes=[code]),
                    actor=actor,
                    redis=real_redis,
                )
            )[0]
            assert new.id != old.id and new.current_route_id != old.current_route_id
            assert new.work_line_id is None
            assert await session.scalar(select(func.count()).select_from(DeviceRoute)) == 2
    finally:
        await real_redis.delete(
            service.origin_key(origin), *("device-preview:" + t for t in tokens)
        )


async def test_preview_owner_expiration_selection_and_unselected_items(asset_factory, real_redis):
    from datetime import timedelta

    from app.schemas.device import PreviewSnapshot

    actor = AssetActor(1, "preview-boundaries", None, None)
    origin = "http://127.0.0.1:45731"

    async def query(origin, **kwargs):
        parsed = EcsStatusResponse.model_validate_json(json.dumps(ecs_status_document("D1", "D2")))
        return StatusQueryResult(
            ecs_origin=origin,
            requested_device_code=None,
            fetched_at=datetime.now(UTC),
            devices=parsed.devices,
        )

    service = DeviceStatusService(asset_factory, real_redis, query_fn=query)
    preview = await service.preview(actor_id=1, ecs_origin=origin)
    key = "device-preview:" + preview.preview_token
    data = DeviceRegisterIn(preview_token=preview.preview_token, device_codes=["D1"])
    try:
        async with asset_factory() as session:
            with pytest.raises(AppError) as owner:
                await register_devices(
                    session,
                    data,
                    actor=AssetActor(2, "preview-boundaries", None, None),
                    redis=real_redis,
                )
            assert owner.value.code == "NOT_FOUND"
            with pytest.raises(AppError) as unknown:
                await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=preview.preview_token, device_codes=["unknown"]),
                    actor=actor,
                    redis=real_redis,
                )
            assert unknown.value.code == "CONFLICT"
            first = (await register_devices(session, data, actor=actor, redis=real_redis))[0]
            second = (
                await register_devices(
                    session,
                    DeviceRegisterIn(preview_token=preview.preview_token, device_codes=["D2"]),
                    actor=actor,
                    redis=real_redis,
                )
            )[0]
            assert first.device_code == "D1" and second.device_code == "D2"
            expired = PreviewSnapshot.model_validate_json(await real_redis.get(key)).model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
            await real_redis.set(key, expired.model_dump_json(), ex=30)
            with pytest.raises(AppError) as stale:
                await register_devices(session, data, actor=actor, redis=real_redis)
            assert stale.value.detail == "PREVIEW_EXPIRED"
    finally:
        await real_redis.delete(key, service.origin_key(origin))
