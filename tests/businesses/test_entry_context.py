from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.integrations.ecs_callback.registry import EcsEventContext
from app.integrations.wms_callback.registry import CallbackContext


def test_callback_context_old_positional_calls_still_work():
    notes = []
    ctx = CallbackContext(None, 1, "identity", 2, {}, notes)
    assert ctx.notes is notes
    assert ctx.runtime is None
    assert CallbackContext(None, 1, "identity", 2, {}).notes == []
    runtime = object()
    assert CallbackContext(None, 1, "id", 2, {}, runtime=runtime).runtime is runtime
    assert (
        EcsEventContext(
            session=None,
            receipt_id=1,
            digest="d",
            device_code="D",
            event_type="SCAN",
            timestamp=1,
            binding=None,
            runtime=runtime,
        ).runtime
        is runtime
    )


def test_mounting_callbacks_preserves_explicit_runtime():
    from app.controllers.ecs_callback import include_ecs_callback
    from app.controllers.wms_events import include_wms_events

    app = FastAPI()
    runtime = object()
    include_ecs_callback(
        app, environment="production", resolve_binding=lambda *a: None, runtime=runtime
    )
    include_wms_events(app, environment="production")
    assert app.state.business_runtime is runtime


@pytest.mark.asyncio
async def test_context_receives_runtime_created_during_lifespan(monkeypatch):
    from contextlib import asynccontextmanager

    from app import main
    from app.businesses.runtime import BusinessRuntime

    definitions = {d.code: d for d in main.catalog.definitions()}
    runtime = BusinessRuntime(
        definitions=definitions, services={code: object() for code in definitions}
    )
    outbox = object()

    @asynccontextmanager
    async def reliable(*args, **kwargs):
        yield outbox

    seen = []

    def build(**kwargs):
        assert kwargs["outbox"] is outbox
        seen.append(kwargs)
        return runtime

    monkeypatch.setattr(main, "reliable_lifespan", reliable)
    monkeypatch.setattr(main, "build_runtime", build)
    monkeypatch.setattr(main, "DeviceStatusService", lambda *a: None)
    monkeypatch.setattr(main, "get_redis", lambda: SimpleNamespace(aclose=_close))
    monkeypatch.setattr(main.get_redis, "cache_clear", lambda: None, raising=False)
    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.business_runtime is runtime
        assert len(seen) == 1


async def _close():
    pass


@pytest.mark.asyncio
async def test_lifespan_preserves_explicit_runtime_and_checks_service_completeness(monkeypatch):
    from contextlib import asynccontextmanager

    from app import main
    from app.businesses import catalog
    from app.businesses.runtime import BusinessRuntime
    from tests.helpers import business_composition

    entry = business_composition()
    runtime = BusinessRuntime(
        definitions={entry.DEFINITION.code: entry.DEFINITION},
        services={entry.DEFINITION.code: object()},
    )

    @asynccontextmanager
    async def reliable(*args, **kwargs):
        yield object()

    def forbidden(**kwargs):
        pytest.fail("explicit business runtime must not be replaced")

    monkeypatch.setattr(main, "reliable_lifespan", reliable)
    monkeypatch.setattr(main, "build_runtime", forbidden)
    monkeypatch.setattr(catalog, "definitions", lambda: (entry.DEFINITION,))
    monkeypatch.setattr(main, "DeviceStatusService", lambda *a: None)
    monkeypatch.setattr(main, "get_redis", lambda: SimpleNamespace(aclose=_close))
    monkeypatch.setattr(main.get_redis, "cache_clear", lambda: None, raising=False)
    app = FastAPI()
    app.state.business_runtime = runtime
    async with main.lifespan(app):
        assert app.state.business_runtime is runtime
    assert app.state.business_runtime is runtime
    app.state.business_runtime = BusinessRuntime(definitions=runtime.definitions, services={})
    with pytest.raises(ValueError, match="complete"):
        async with main.lifespan(app):
            pytest.fail("missing service must stop before serving")


@pytest.mark.asyncio
async def test_lifespan_factory_receives_only_the_available_platform_capabilities(monkeypatch):
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from app import main
    from app.businesses import catalog
    from app.integrations.ecs_callback import registry
    from tests.helpers import business_composition

    entry = business_composition()
    entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(registry, "_registry", {})
    catalog.validate_business_contracts()
    outbox, service = object(), object()
    app = FastAPI()

    @asynccontextmanager
    async def reliable(*args, **kwargs):
        yield outbox

    def build(capabilities):
        assert app.state.outbox is outbox
        assert capabilities.ecs is app.state.ecs_commands
        assert capabilities.wms.outbox is capabilities.transport.outbox is outbox
        return service

    entry.build_service = build
    monkeypatch.setattr(main, "reliable_lifespan", reliable)
    monkeypatch.setattr(main, "DeviceStatusService", lambda *a: None)
    monkeypatch.setattr(main, "get_redis", lambda: SimpleNamespace(aclose=_close))
    monkeypatch.setattr(main.get_redis, "cache_clear", lambda: None, raising=False)
    async with main.lifespan(app):
        assert app.state.business_runtime.services == {entry.DEFINITION.code: service}
    assert app.state.business_runtime is None
