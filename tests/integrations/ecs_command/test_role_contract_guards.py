"""Role registration and runtime authorization fail closed through public entry points."""

from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.controllers.ecs_callback import include_ecs_callback
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import CallbackPayload, registry
from app.integrations.ecs_command import CommandBinding, RoleRoutes, configure_ecs
from app.integrations.ecs_command.dev import (
    PROTOCOL_ANNEX,
    PROTOCOL_MODELS,
    DevEcsRoutes,
    LocalEcsRoutes,
    configure_local_ecs,
)
from app.integrations.ecs_command.routing import RouteAuthorization
from app.models import CallbackReceipt
from tests.helpers import TrialPayload, ecs_binding, ecs_history_annexes


@pytest.fixture
def runtime():
    state = SimpleNamespace(
        origin="http://127.0.0.1:45871", route_id="device.route." + str(uuid7())
    )

    async def binding(session, code):
        return state.binding

    async def route(session, bound):
        return state.origin, state.route_id

    state.resolve_binding, state.resolve_route = binding, route
    state.entry = CommandBinding("SCAN1", "MOVE", "trial", "1", TrialPayload)
    state.annexes = ecs_history_annexes()
    state.routes = RoleRoutes(
        [state.entry], annexes=state.annexes, resolve_binding=binding, resolve_route=route
    )
    return state


def test_invalid_event_role_cannot_reserve_a_registration(monkeypatch):
    for role in [None, 1, "", "scan1", "SCAN-1"]:
        monkeypatch.setattr(registry, "_registry", {})

        async def handler(ctx, payload):
            pass

        with pytest.raises(ValueError, match="device role"):
            registry.ecs_event("SCAN_COMPLETED", device_role=role, data=CallbackPayload)(handler)
        registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)(handler)
        assert registry.lookup("SCAN1", "SCAN_COMPLETED").func is handler


def test_command_declaration_requires_an_explicit_valid_contract():
    for changes in [
        dict(device_role=None),
        dict(task_type="move"),
        dict(annex_id=""),
        dict(annex_version=None),
    ]:
        values = dict(device_role="SCAN1", task_type="MOVE", annex_id="trial", annex_version="1")
        with pytest.raises(ValueError):
            CommandBinding(**(values | changes), source=TrialPayload)


def test_role_contract_assembly_rejects_duplicates_and_missing_annexes(runtime):
    kwargs = dict(resolve_binding=runtime.resolve_binding, resolve_route=runtime.resolve_route)
    with pytest.raises(TypeError, match="CommandBinding"):
        RoleRoutes([object()], annexes=runtime.annexes, **kwargs)
    with pytest.raises(ValueError, match="duplicate command binding.*TrialPayload"):
        RoleRoutes([runtime.entry, runtime.entry], annexes=runtime.annexes, **kwargs)
    with pytest.raises(ValueError, match="confirmed annex"):
        RoleRoutes([runtime.entry], annexes={}, **kwargs)
    application = FastAPI()
    with pytest.raises(ValueError, match="confirmed annex"):
        configure_ecs(application, routes=runtime.routes, annexes={})
    assert not hasattr(application.state, "ecs_commands")


def test_local_role_configuration_cannot_be_silently_ignored_in_production(runtime):
    application = FastAPI()
    with pytest.raises(ValueError, match="ENVIRONMENT=local"):
        configure_local_ecs(application, environment="production", routes=runtime.routes)
    assert not hasattr(application.state, "ecs_commands")


async def test_damaged_frozen_routes_do_not_consult_current_bindings(runtime):
    for change in [
        {"route_id": "device.route." + str(UUID(int=0))},
        {"route_id": "device.route.invalid"},
        {"route_id": "legacy"},
        {"version": None},
        {"commands": None},
        {"commands": []},
        {"commands": ["MOVE", "MOVE"]},
        {"commands": [1]},
        {"annex_id": ""},
        {"device_code": None},
        {"retry_window": True},
        {"retry_window": 301},
        {"origin": "http://127.0.0.1:45871/"},
        {"unexpected": "field"},
    ]:
        runtime.binding = await ecs_binding(None, "D1")
        route = await runtime.routes.resolve(None, "D1", "MOVE")
        frozen = asdict(route) | {"retry_window": route.retry_window.total_seconds()}

        async def forbidden(*args):
            pytest.fail("damaged snapshots must stop before runtime lookup")

        guarded_routes = RoleRoutes(
            [runtime.entry],
            annexes=runtime.annexes,
            resolve_binding=forbidden,
            resolve_route=forbidden,
        )
        for invalid in (None, {}, frozen | change):
            assert (
                await guarded_routes.authorize_frozen(None, invalid) == RouteAuthorization.UNKNOWN
            )


async def test_changed_runtime_identity_never_reauthorizes_an_old_command(runtime):
    for change in ["device", "role", "device_version", "origin", "route"]:
        runtime.binding = await ecs_binding(None, "D1")
        route = await runtime.routes.resolve(None, "D1", "MOVE")
        frozen = asdict(route) | {"retry_window": route.retry_window.total_seconds()}
        if change == "device":
            runtime.binding = replace(runtime.binding, device_code="OTHER")
            expected = RouteAuthorization.UNKNOWN
        else:
            expected = RouteAuthorization.REVOKED
            if change == "role":
                runtime.binding = replace(runtime.binding, device_role="SCAN4")
            elif change == "device_version":
                runtime.binding = replace(runtime.binding, device_version=2)
            elif change == "origin":
                runtime.origin = "http://127.0.0.1:45872"
            else:
                runtime.route_id = "device.route." + str(uuid7())
        assert await runtime.routes.authorize_frozen(None, frozen) == expected


@pytest.mark.parametrize("failed_port", ["binding", "route"])
async def test_runtime_infrastructure_failures_are_retryable_errors(runtime, failed_port):
    runtime.binding = await ecs_binding(None, "D1")
    route = await runtime.routes.resolve(None, "D1", "MOVE")
    frozen = asdict(route) | {"retry_window": route.retry_window.total_seconds()}

    async def failed(*args):
        raise RuntimeError("infrastructure unavailable")

    routes = RoleRoutes(
        [runtime.entry],
        annexes=runtime.annexes,
        resolve_binding=failed if failed_port == "binding" else runtime.resolve_binding,
        resolve_route=failed if failed_port == "route" else runtime.resolve_route,
    )
    with pytest.raises(RuntimeError, match="infrastructure unavailable"):
        await routes.resolve(None, "D1", "MOVE")
    with pytest.raises(RuntimeError, match="infrastructure unavailable"):
        await routes.authorize_frozen(None, frozen)


async def test_injected_event_resolver_failure_returns_503_then_retries_original_event(
    callback_store, monkeypatch
):
    monkeypatch.setattr(registry, "_registry", {})
    handled = []

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=TrialEvent)
    async def handler(ctx, payload):
        handled.append((ctx.work_line_id, payload.qty))

    unavailable = True

    async def resolve(session, code):
        if unavailable:
            raise RuntimeError("binding infrastructure unavailable")
        return await ecs_binding(session, code)

    application = FastAPI()
    application.state.callback_session_factory = callback_store
    include_ecs_callback(application, environment="production", resolve_binding=resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        body = dict(device_code="D1", event_type="SCAN_COMPLETED", timestamp=1, data={"qty": 1})
        response = await client.post("/api/v1/callback/event", json=body)
        assert response.status_code == 503
        async with callback_store() as session:
            assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 0
        unavailable = False
        for _ in range(2):
            response = await client.post("/api/v1/callback/event", json=body)
            assert response.status_code == 200
        assert handled == [(1, 1)]


class TrialEvent(CallbackPayload):
    qty: int


async def test_local_event_example_does_not_replace_the_injected_formal_binding(
    callback_store, monkeypatch
):
    from app.integrations.ecs_callback.dev_ecs_event import ExampleScan, example_scan

    monkeypatch.setattr(registry, "_registry", {})
    registry.ecs_event("EXAMPLE_SCAN", device_role="EXAMPLE_SCAN", data=ExampleScan)(example_scan)
    handled = []

    @registry.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=TrialEvent)
    async def handler(ctx, payload):
        handled.append(ctx.device_code)

    application = FastAPI()
    application.state.callback_session_factory = callback_store
    include_ecs_callback(application, environment="local", resolve_binding=ecs_binding)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        for code, event, data in (
            ("LOCAL_EXAMPLE_SCAN", "EXAMPLE_SCAN", {"barcode": "B1"}),
            ("D1", "SCAN_COMPLETED", {"qty": 1}),
        ):
            response = await client.post(
                "/api/v1/callback/event",
                json=dict(device_code=code, event_type=event, timestamp=1, data=data),
            )
            assert response.status_code == 200
    assert handled == ["D1"]


async def test_debug_http_entrypoint_preserves_formal_routes_and_one_outbox(
    ecs, runtime, client, admin, monkeypatch
):
    from app.integrations.ecs_command.runtime import TARGET
    from app.main import app

    runtime.binding = await ecs_binding(None, "D1")
    ecs.service.routes = LocalEcsRoutes(runtime.routes, DevEcsRoutes())
    ecs.service.annexes.update(runtime.annexes | {PROTOCOL_ANNEX: PROTOCOL_MODELS})
    ecs.service.target = TARGET
    ecs.targets[TARGET] = replace(ecs.targets["ecs"], name=TARGET)
    monkeypatch.setattr(app.state, "ecs_commands", ecs.service, raising=False)
    monkeypatch.setattr(app.state, "outbox", ecs.outbox, raising=False)
    body = dict(
        client_request_id="local-roles",
        ecs_url="http://127.0.0.1:45872",
        device_code="D1",
        task_type="UNREGISTERED_TASK",
        timeout_ms=30000,
        params={"custom": True},
    )
    first = await client.post("/api/v1/dev/ecs/commands", json=body, headers=admin)
    assert first.status_code == 200, first.text
    replay = await client.post("/api/v1/dev/ecs/commands", json=body, headers=admin)
    assert (
        replay.status_code == 200 and replay.json()["command_code"] == first.json()["command_code"]
    )
    formal = await ecs.service.routes.resolve(None, "D1", "MOVE")
    assert (formal.annex_id, formal.annex_version) == ("trial", "1")


async def test_malformed_frozen_origin_is_unknown_before_runtime_lookup(runtime):
    runtime.binding = await ecs_binding(None, "D1")
    route = await runtime.routes.resolve(None, "D1", "MOVE")
    frozen = asdict(route) | {
        "retry_window": route.retry_window.total_seconds(),
        "origin": "http://127.0.0.1:bad",
    }

    async def forbidden(*args):
        pytest.fail("malformed frozen origins must stop before runtime lookup")

    routes = RoleRoutes(
        [runtime.entry],
        annexes=runtime.annexes,
        resolve_binding=forbidden,
        resolve_route=forbidden,
    )
    assert await routes.authorize_frozen(None, frozen) == RouteAuthorization.UNKNOWN
