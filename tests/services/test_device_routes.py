from dataclasses import asdict

import pytest

from app.core.identifiers import uuid7
from app.integrations.ecs_command import CommandBinding
from app.integrations.ecs_command.routing import RouteAuthorization
from app.models import Device, DeviceRoute
from app.schemas.workline import WorkLineUpdate
from app.services.asset_audit import AssetActor
from app.services.device_routes import DeviceRoutes
from app.services.device_service import delete_device
from app.services.workline_service import update_workline
from tests.helpers import TrialPayload, add_bound_device, ecs_history_annexes


def routes():
    return DeviceRoutes(
        [CommandBinding("SCAN1", "MOVE", "trial", "1", TrialPayload)],
        annexes=ecs_history_annexes(),
    )


async def test_deleted_configuration_cannot_authorize_another_send(db):
    line, device, route = await add_bound_device(db, "D1", "http://127.0.0.1:12345")
    await db.commit()
    provider = routes()
    frozen = await provider.resolve(db, "D1", "MOVE")
    wire = {**asdict(frozen), "retry_window": frozen.retry_window.total_seconds()}
    assert await provider.authorize_frozen(db, wire) == RouteAuthorization.ALLOW
    await update_workline(
        db,
        line.id,
        WorkLineUpdate(line_name=line.line_name, version=line.version, device_bindings={}),
        actor=AssetActor(1, "routes", None, None),
    )
    await delete_device(db, device.id, version=1, actor=AssetActor(1, "routes", None, None))
    assert await provider.authorize_frozen(db, wire) == RouteAuthorization.REVOKED
    with pytest.raises(ValueError):
        await provider.resolve(db, "D1", "MOVE")
    assert (
        await provider.authorize_frozen(db, {**wire, "version": "1"}) == RouteAuthorization.UNKNOWN
    )
    assert await db.get(DeviceRoute, route.id) is not None


async def test_new_route_resolution_refreshes_preloaded_configuration(asset_pg):
    factory, code = asset_pg
    async with factory.begin() as session:
        line, device, old = await add_bound_device(
            session, code, "http://127.0.0.1:45811", line_code=code + "-line"
        )
        new = DeviceRoute(id=str(uuid7()), device_code=code, ecs_origin="http://127.0.0.1:45812")
        session.add(new)
        await session.flush()
        device_id, new_id = device.id, new.id
    async with factory() as caller:
        preloaded = await caller.get(Device, device_id)
        async with factory.begin() as writer:
            current = await writer.get(Device, device_id, with_for_update=True)
            current.current_route_id = new_id
            current.version += 1
        resolved = await routes().resolve(caller, code, "MOVE")
        assert resolved.origin == "http://127.0.0.1:45812"
        assert preloaded.version == 2


async def test_configuration_restoration_does_not_restore_original_permission(db):
    line, device, _ = await add_bound_device(db, "D1", "http://127.0.0.1:12345")
    await db.commit()
    provider = routes()
    frozen = await provider.resolve(db, "D1", "MOVE")
    wire = {**asdict(frozen), "retry_window": frozen.retry_window.total_seconds()}
    original_name = line.line_name
    for name in ("changed", original_name):
        line = await update_workline(
            db,
            line.id,
            WorkLineUpdate(line_name=name, version=line.version),
            actor=AssetActor(1, "change", None, None),
        )
        assert line.device_bindings == {"SCAN1": "D1"}
        assert await provider.authorize_frozen(db, wire) == RouteAuthorization.REVOKED


def test_role_task_declarations_reject_duplicates_and_missing_annexes():
    entry = CommandBinding("SCAN1", "MOVE", "trial", "1", TrialPayload)
    with pytest.raises(ValueError, match="duplicate.*TrialPayload"):
        DeviceRoutes([entry, entry], annexes=ecs_history_annexes())
    with pytest.raises(ValueError, match="confirmed annex"):
        DeviceRoutes([entry])


def test_missing_annex_does_not_partially_assemble_ecs():
    from fastapi import FastAPI

    from app.integrations.ecs_command import configure_ecs

    app = FastAPI()
    with pytest.raises(ValueError, match="annex"):
        configure_ecs(app, routes=routes(), annexes={})
    assert getattr(app.state, "ecs_commands", None) is None


@pytest.mark.parametrize("missing", ["origin", "annex_id", "retry_window"])
async def test_incomplete_frozen_route_is_unknown_without_authorizing(db, missing):
    await add_bound_device(db, "D1", "http://127.0.0.1:12345")
    await db.commit()
    provider = routes()
    frozen = await provider.resolve(db, "D1", "MOVE")
    wire = {**asdict(frozen), "retry_window": frozen.retry_window.total_seconds()}
    del wire[missing]
    assert await provider.authorize_frozen(db, wire) == RouteAuthorization.UNKNOWN


@pytest.mark.parametrize(
    "corruption", [{"origin": "not-an-origin"}, {"commands": [""]}, {"device_code": ""}]
)
async def test_damaged_origin_and_task_are_unknown_before_binding_resolution(db, corruption):
    await add_bound_device(db, "D1", "http://127.0.0.1:12345")
    await db.commit()
    provider = routes()
    frozen = await provider.resolve(db, "D1", "MOVE")
    wire = {**asdict(frozen), "retry_window": frozen.retry_window.total_seconds(), **corruption}
    assert await provider.authorize_frozen(db, wire) == RouteAuthorization.UNKNOWN
