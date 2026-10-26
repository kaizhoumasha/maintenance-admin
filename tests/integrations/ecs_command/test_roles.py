"""Role contracts with injected runtime bindings, without a device directory or Status."""

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select

from app.core.identifiers import uuid7
from app.integrations.ecs_callback import CallbackPayload, registry
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.integrations.ecs_command import CommandBinding, DeviceAnnex, RoleRoutes
from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.ecs_command.routing import RouteAuthorization
from app.models import EcsCommand, ReliableMessage
from tests.helpers import loopback_app, simulator_sender

SCAN1 = "SCAN1"
SCAN4 = "SCAN4"


class ScanData(CallbackPayload):
    qty: int


@pytest.fixture
def role_runtime():
    state = SimpleNamespace(
        binding=DeviceBinding(
            device_id=1,
            device_code="D1",
            device_version=1,
            work_line_id=1,
            work_line_version=1,
            device_role=SCAN1,
            device_bindings={SCAN1: "D1"},
            position_bindings={"INLET": "P1"},
        ),
        origin="http://127.0.0.1:45871",
        route_id="device.route." + str(uuid7()),
        calls=0,
    )

    async def resolve_binding(session, device_code):
        state.calls += 1
        return state.binding if device_code == "D1" else None

    async def resolve_route(session, binding):
        return state.origin, state.route_id

    state.resolve_binding = resolve_binding
    state.resolve_route = resolve_route
    state.annexes = {("scan", "1"): DeviceAnnex(ScanData, ScanData, ErrorDetail, 1, 60000)}
    state.routes = RoleRoutes(
        [CommandBinding(SCAN1, "MOVE", "scan", "1", ScanData)],
        annexes=state.annexes,
        resolve_binding=resolve_binding,
        resolve_route=resolve_route,
    )
    return state


def test_role_declaration_does_not_resolve_runtime_devices(role_runtime):
    assert role_runtime.calls == 0
    assert set(role_runtime.routes.bindings) == {(SCAN1, "MOVE")}


async def test_role_route_selects_contract_only_after_runtime_resolution(role_runtime):
    route = await role_runtime.routes.resolve(None, "D1", "MOVE")
    assert role_runtime.calls == 1
    assert (route.device_code, route.annex_id, route.origin) == ("D1", "scan", role_runtime.origin)
    for device, task in (("UNKNOWN", "MOVE"), ("D1", "UNKNOWN")):
        with pytest.raises(ValueError):
            await role_runtime.routes.resolve(None, device, task)
    role_runtime.binding = replace(role_runtime.binding, device_role=SCAN4)
    with pytest.raises(ValueError, match="annex"):
        await role_runtime.routes.resolve(None, "D1", "MOVE")
    role_runtime.binding = replace(role_runtime.binding, device_code="OTHER")
    with pytest.raises(ValueError, match="binding.*device"):
        await role_runtime.routes.resolve(None, "D1", "MOVE")


@pytest.mark.parametrize(
    "route_id",
    [
        None,
        1,
        "",
        "device.route.invalid",
        "other.019b1234-abcd-7abc-8def-123456789abc",
        "device.route.019b1234-abcd-4abc-8def-123456789abc",
        "device.route.019B1234-ABCD-7ABC-8DEF-123456789ABC",
        "device.route.019b1234abcd7abc8def123456789abc",
    ],
)
async def test_invalid_role_routes_do_not_persist_commands(ecs, role_runtime, route_id):
    ecs.service.routes = role_runtime.routes
    ecs.service.annexes.update(role_runtime.annexes)
    role_runtime.route_id = route_id
    arguments = dict(
        owner=ecs.owner,
        call_key="invalid-role-route",
        device_code="D1",
        task_type="MOVE",
        timeout_ms=30000,
        params={"qty": 1},
    )
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(session, **arguments)
        assert (
            await ecs.service.get_by_call(session, owner=ecs.owner, call_key=arguments["call_key"])
            is None
        )
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target == "ecs")
            )
            == 0
        )
        role_runtime.route_id = "device.route.019b1234-abcd-7abc-8def-123456789abc"
        command = await ecs.service.create(session, **arguments)
    async with ecs.factory.begin() as session:
        stored = await ecs.service.get_by_call(
            session, owner=ecs.owner, call_key=arguments["call_key"]
        )
        assert stored.command_code == command.command_code
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target == "ecs")
            )
            == 1
        )


async def test_role_route_revocation_and_damaged_snapshots(role_runtime):
    routes = role_runtime.routes
    route = await routes.resolve(None, "D1", "MOVE")
    frozen = {**asdict(route), "retry_window": route.retry_window.total_seconds()}
    assert await routes.authorize_frozen(None, frozen) == RouteAuthorization.ALLOW
    for change in ({"version": "1"}, {"origin": "invalid"}, {"commands": [""]}):
        calls = role_runtime.calls
        assert await routes.authorize_frozen(None, frozen | change) == RouteAuthorization.UNKNOWN
        assert role_runtime.calls == calls
    original = role_runtime.binding
    for version in (2, 3):
        role_runtime.binding = replace(original, work_line_version=version)
        assert await routes.authorize_frozen(None, frozen) == RouteAuthorization.REVOKED
    role_runtime.binding = None
    assert await routes.authorize_frozen(None, frozen) == RouteAuthorization.REVOKED


async def test_shared_task_uses_each_roles_strict_contract_without_a_directory(ecs, role_runtime):
    class ExitData(CallbackPayload):
        exit_code: str

    annexes = role_runtime.annexes | {
        ("exit", "1"): DeviceAnnex(ExitData, ExitData, ErrorDetail, 1, 60000)
    }
    ecs.service.annexes.update(annexes)
    ecs.service.routes = RoleRoutes(
        [
            CommandBinding(SCAN1, "MOVE", "scan", "1", ScanData),
            CommandBinding(SCAN4, "MOVE", "exit", "1", ExitData),
        ],
        annexes=annexes,
        resolve_binding=role_runtime.resolve_binding,
        resolve_route=role_runtime.resolve_route,
    )

    async def create(session, key, params):
        return await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key=key,
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params=params,
        )

    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await create(session, "bad-entry", {"exit_code": "RETURN"})
        entry = await create(session, "entry", {"qty": 1})
        role_runtime.binding = replace(
            role_runtime.binding,
            device_role=SCAN4,
            work_line_version=2,
            device_bindings={SCAN4: "D1"},
        )
        with pytest.raises(ValueError):
            await create(session, "bad-exit", {"qty": 1})
        exit_command = await create(session, "exit", {"exit_code": "RETURN"})
        assert entry.command_code != exit_command.command_code
        assert (
            await ecs.service.get_by_call(session, owner=ecs.owner, call_key="entry")
        ).command_code == entry.command_code


async def test_injected_event_binding_dispatches_constants_and_replays_original_receipt(
    role_runtime, callback_store, monkeypatch
):
    monkeypatch.setattr(registry, "_registry", {})
    handled = []

    @registry.ecs_event("SCAN_COMPLETED", device_role=SCAN1, data=ScanData)
    async def scan(ctx, payload):
        handled.append((ctx.device_role, ctx.binding.position_bindings["INLET"], payload.qty))

    assert role_runtime.calls == 0
    raw = json.dumps(
        dict(device_code="D1", event_type="SCAN_COMPLETED", timestamp=1, data={"qty": 2})
    ).encode()
    async with callback_store.begin() as session:
        assert (
            await receive_ecs_event(session, raw, resolve_binding=role_runtime.resolve_binding)
        ).status_code == 200
    role_runtime.binding = None
    calls = role_runtime.calls
    async with callback_store.begin() as session:
        assert (
            await receive_ecs_event(session, raw, resolve_binding=role_runtime.resolve_binding)
        ).outcome == "duplicate"
    assert role_runtime.calls == calls
    assert handled == [(SCAN1, "P1", 2)]


async def test_role_sender_uses_injected_origin_and_blocks_outdated_configuration(
    ecs, role_runtime
):
    peer = FastAPI()
    received = []

    @peer.post("/api/v1/device/command")
    async def acknowledge(request: Request):
        received.append(await request.json())
        return {"code": 200, "message": "ACK"}

    ecs.service.routes = role_runtime.routes
    ecs.service.annexes.update(role_runtime.annexes)

    async def create(key):
        async with ecs.factory.begin() as session:
            return await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key=key,
                device_code="D1",
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )

    async with loopback_app(peer) as origin:
        role_runtime.origin = origin
        old = await create("old")
        role_runtime.binding = replace(role_runtime.binding, work_line_version=2)
        async with simulator_sender(ecs, {origin}) as sender:
            await sender.tick()
            async with asyncio.timeout(5):
                await asyncio.gather(*list(sender.inflight))
            assert received == []
            current = await create("current")
            await sender.tick()
            async with asyncio.timeout(5):
                await asyncio.gather(*list(sender.inflight))
        assert [request["command_code"] for request in received] == [current.command_code]
        async with ecs.factory() as session:
            previous = await ecs.service.get_command(
                session, owner=ecs.owner, command_code=old.command_code
            )
            assert previous.hold_reason == "AUTHORIZATION_REVOKED"
            assert (
                await ecs.service.get_by_call(session, owner=ecs.owner, call_key="old")
            ).command_code == old.command_code


async def test_role_configuration_revocation_retains_original_result(ecs, role_runtime):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.reliable.store import MessageStore

    ecs.service.routes = role_runtime.routes
    ecs.service.annexes.update(role_runtime.annexes)
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="late-role",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])
    role_runtime.binding = None
    assert not await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory.begin() as session:
        result = await receive_ecs_result(
            session,
            json.dumps(
                dict(
                    command_code=command.command_code,
                    device_code="D1",
                    result="SUCCESS",
                    finish_time=1,
                    data={"qty": 1},
                )
            ).encode(),
            ecs.service,
        )
        assert result.status_code == 200


async def test_local_protocol_and_formal_role_routes_share_one_service(role_runtime):
    from app.integrations.ecs_command.dev import PROTOCOL_ANNEX, configure_local_ecs

    app = FastAPI()
    service = configure_local_ecs(
        app,
        environment="local",
        routes=role_runtime.routes,
        history_annexes=role_runtime.annexes,
    )
    assert service is app.state.ecs_commands
    assert role_runtime.calls == 0
    formal = await service.routes.resolve(None, "D1", "MOVE")
    debug = await service.routes.for_origin("http://127.0.0.1:45872").resolve(
        None, "D1", "UNREGISTERED_TASK"
    )
    assert (formal.annex_id, formal.annex_version) == ("scan", "1")
    assert (debug.annex_id, debug.annex_version) == PROTOCOL_ANNEX
    for snapshot in (formal, debug):
        frozen = {**asdict(snapshot), "retry_window": snapshot.retry_window.total_seconds()}
        assert await service.routes.authorize_frozen(None, frozen) == RouteAuthorization.ALLOW


async def test_registry_is_available_without_status_capabilities(client, admin, monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "integration_capabilities", (), raising=False)
    response = await client.get("/api/v1/dev/integrations/registry", headers=admin)
    assert response.status_code == 200
    assert all(row["kind"] != "ecs_status" for row in response.json())


def test_role_and_debug_entrypoints_do_not_import_status_or_device_services():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys

blocked = (
    'app.integrations.ecs_status', 'app.controllers.ecs_status',
    'app.services.device_routes', 'app.services.device_bindings',
    'app.services.device_status_service',
)

class NoDeviceServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('role/debug framework imported ' + fullname)

sys.meta_path.insert(0, NoDeviceServices())
from app.integrations.ecs_command import CommandBinding, RoleRoutes, configure_ecs
from app.controllers.ecs_callback import include_ecs_callback
from app.controllers.dev_ecs_command import router
from app.controllers.dev_registry import list_registrations
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
