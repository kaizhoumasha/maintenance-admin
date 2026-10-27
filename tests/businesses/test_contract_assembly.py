from dataclasses import replace

import pytest
from pydantic import BaseModel

from app.businesses import catalog
from app.businesses.definitions import InitialEntry
from app.integrations.ecs_callback import CallbackPayload
from app.integrations.ecs_callback import registry as ecs_registry
from app.integrations.ecs_command import CommandBinding, DeviceAnnex
from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.registration import RegistrationSource
from app.integrations.wms_callback import registry as wms_registry
from app.integrations.wms_request import operation as requests
from tests.helpers import business_composition


class Params(BaseModel):
    pass


ANNEX = DeviceAnnex(Params, Params, ErrorDetail, 1, 100)
KEY = ("test.move", "1")


@pytest.fixture
def composition(monkeypatch):
    entry = business_composition()
    entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(ecs_registry, "_registry", {})
    monkeypatch.setattr(wms_registry, "_registry", {})
    monkeypatch.setattr(requests, "_registry", {})
    return entry


def command(role="TEST_SCAN"):
    return CommandBinding(role, "MOVE", *KEY, Params)


def register_event(entry, role="TEST_SCAN", *, module=None, local=False):
    async def handler(ctx, payload):
        pass

    source = RegistrationSource(
        module or entry.__name__.rsplit(".", 1)[0] + ".ecs_event", "ecs_event.py", "handler", local
    )
    ecs_registry._registry[(role, "SCAN_COMPLETED")] = ecs_registry.Registered(
        handler, CallbackPayload, source
    )


def test_command_role_must_belong_to_its_business(composition):
    composition.COMMAND_BINDINGS = (command("OTHER_ROLE"),)
    composition.ANNEXES = {KEY: ANNEX}
    with pytest.raises(ValueError, match="test_sorting.*OTHER_ROLE"):
        catalog.validate_business_contracts()


def test_registered_event_role_must_belong_to_its_business(composition):
    register_event(composition, "OTHER_ROLE")
    with pytest.raises(ValueError, match="test_sorting.*OTHER_ROLE"):
        catalog.validate_business_contracts()


def test_command_requires_its_declared_annex(composition, monkeypatch):
    other = business_composition("other", "OTHER")
    other.DEFINITION = replace(other.DEFINITION, initial_entries=())
    other.ANNEXES = {KEY: ANNEX}
    composition.COMMAND_BINDINGS = (command(),)
    monkeypatch.setattr(catalog, "entries", lambda: (composition, other))
    with pytest.raises(ValueError, match="test_sorting.*test.move"):
        catalog.validate_business_contracts()


@pytest.mark.parametrize("kind", ["ecs_event", "wms_callback"])
@pytest.mark.parametrize("source", ["missing", "other", "prefix", "dev", "wrong_role", "request"])
def test_initial_entry_requires_own_formal_registration(composition, kind, source):
    initial = InitialEntry(
        kind=kind,
        name="SCAN_COMPLETED" if kind == "ecs_event" else "scan.done@v1",
        device_role="TEST_SCAN" if kind == "ecs_event" else None,
    )
    composition.DEFINITION = replace(composition.DEFINITION, initial_entries=(initial,))
    if source == "request" and kind == "wms_callback":
        source_info = RegistrationSource(
            "app.businesses.test_sorting.request", "request.py", "Payload", False
        )
        requests._registry[initial.name] = requests.Registered(
            initial.name, requests.Endpoint.FACTS, requests.RequestPayload, None, None, source_info
        )
    if source not in ("missing", "request"):
        module = "app.businesses.test_sorting.ecs_event"
        if source == "other":
            module = "app.businesses.other.ecs_event"
        elif source == "prefix":
            module = "app.businesses.test_sorting_other.ecs_event"
        elif source == "dev":
            module = "app.businesses.test_sorting.dev_ecs_event"
        register_event(
            composition,
            "OTHER" if source == "wrong_role" else "TEST_SCAN",
            module=module,
            local=source == "dev",
        )
        if kind == "wms_callback" and source != "wrong_role":
            wms_registry._registry[initial.name] = ecs_registry._registry[
                ("TEST_SCAN", "SCAN_COMPLETED")
            ]
    with pytest.raises(ValueError, match="test_sorting"):
        catalog.validate_business_contracts()


def test_duplicate_command_keys_fail_assembly(composition, monkeypatch):
    other = business_composition("other")
    other.DEFINITION = replace(other.DEFINITION, initial_entries=())
    for entry in (composition, other):
        entry.COMMAND_BINDINGS = (command(),)
        entry.ANNEXES = {KEY: ANNEX}
    monkeypatch.setattr(catalog, "entries", lambda: (composition, other))
    with pytest.raises(ValueError, match="duplicate.*TEST_SCAN.*MOVE"):
        catalog.validate_business_contracts()


@pytest.mark.parametrize(
    "field", ["params_model", "result_model", "error_model", "timeout_min_ms", "timeout_max_ms"]
)
def test_conflicting_annex_keys_fail_before_resource_creation(composition, monkeypatch, field):
    other = business_composition("other", "OTHER")
    other.DEFINITION = replace(other.DEFINITION, initial_entries=())
    composition.ANNEXES = {KEY: ANNEX}
    value = (
        type("Other", (ErrorDetail if field == "error_model" else Params,), {})
        if field.endswith("model")
        else 2
    )
    other.ANNEXES = {KEY: replace(ANNEX, **{field: value})}
    monkeypatch.setattr(catalog, "entries", lambda: (composition, other))
    with pytest.raises(ValueError, match="other.*test.move"):
        catalog.validate_business_contracts()


def test_identical_annex_contract_can_be_shared(composition, monkeypatch):
    other = business_composition("other", "OTHER")
    other.DEFINITION = replace(other.DEFINITION, initial_entries=())
    composition.COMMAND_BINDINGS = (command(),)
    other.COMMAND_BINDINGS = (command("OTHER"),)
    composition.ANNEXES = {KEY: ANNEX}
    other.ANNEXES = {KEY: replace(ANNEX)}
    monkeypatch.setattr(catalog, "entries", lambda: (composition, other))
    commands, annexes = catalog.validate_business_contracts()
    assert commands == composition.COMMAND_BINDINGS + other.COMMAND_BINDINGS
    assert annexes == {KEY: ANNEX}


def test_definition_query_does_not_crossvalidate_or_load_entries(composition):
    composition.COMMAND_BINDINGS = (command("OTHER"),)
    assert catalog.definitions() == (composition.DEFINITION,)


@pytest.mark.parametrize("kind", ["ecs_event", "wms_callback"])
def test_own_initial_entry_is_valid(composition, kind):
    register_event(composition)
    if kind == "ecs_event":
        initial = InitialEntry(kind=kind, name="SCAN_COMPLETED", device_role="TEST_SCAN")
    else:
        from app.integrations.wms_callback.operation import Registered

        source = RegistrationSource(
            "app.businesses.test_sorting.callback", "callback.py", "handler", False
        )
        item = ecs_registry._registry[("TEST_SCAN", "SCAN_COMPLETED")]
        wms_registry._registry["scan.done@v1"] = Registered(
            item.func, CallbackPayload, None, (), source
        )
        initial = InitialEntry(kind=kind, name="scan.done@v1")
    composition.DEFINITION = replace(composition.DEFINITION, initial_entries=(initial,))
    assert catalog.validate_business_contracts() == ((), {})


def test_history_annex_conflict_never_constructs_communication_resources(monkeypatch):
    from fastapi import FastAPI

    from app.integrations.ecs_command import dev

    def forbidden(*args, **kwargs):
        pytest.fail("conflict must fail before ECS communication assembly")

    monkeypatch.setattr(dev, "configure_ecs", forbidden)
    with pytest.raises(ValueError, match="conflicting annex"):
        dev.configure_local_ecs(
            FastAPI(),
            environment="local",
            history_annexes={KEY: ANNEX},
            annexes={KEY: replace(ANNEX, timeout_max_ms=101)},
        )


async def test_history_annex_does_not_grant_new_role_command_permission(monkeypatch):
    from fastapi import FastAPI

    from app.integrations.ecs_command.dev import configure_local_ecs
    from app.services.device_routes import DeviceRoutes
    from tests.helpers import ecs_binding

    monkeypatch.setattr("app.services.device_routes.resolve_binding", ecs_binding)
    service = configure_local_ecs(
        FastAPI(), environment="local", history_annexes={KEY: ANNEX}, routes=DeviceRoutes()
    )
    assert service.annexes[KEY] is ANNEX
    assert service.routes.bindings == {}
    with pytest.raises(ValueError, match="Role command annex"):
        # No role exists; retaining the result contract does not authorize a new action.
        await service.routes.resolve(None, "NO_DEVICE", "MOVE")


def test_invalid_contract_never_constructs_routes_sender_or_factory(tmp_path):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from tests.helpers import business_composition
from app.businesses import catalog
from app.integrations import discovery
from app.services import device_routes
from app.reliable import sender
entry = business_composition()
catalog.entries = lambda: (entry,)
def forbidden(*args, **kwargs): raise AssertionError("resources/local/factory reached")
discovery.load_formal_modules = lambda: None
discovery.load_local_modules = forbidden
device_routes.DeviceRoutes = forbidden
sender.Sender = forbidden
entry.build_service = forbidden
try:
    import app.main
except ValueError as error:
    assert "test_sorting" in str(error)
    import sys
    assert 'app.controllers.dev' not in sys.modules
    assert 'app.integrations.ecs_command.dev' not in sys.modules
else:
    raise AssertionError("invalid business served")
""",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ENVIRONMENT": "local"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
