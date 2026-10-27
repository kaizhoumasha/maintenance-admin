from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.businesses import catalog
from tests.helpers import business_composition


@pytest.fixture(autouse=True)
def isolated_validation(monkeypatch):
    monkeypatch.setattr(catalog, "_validated_entries", None)


def test_factories_share_platform_capabilities_without_job_state(monkeypatch):
    from app.businesses.runtime import build_runtime

    first, second = business_composition("first"), business_composition("second")
    seen = []
    for entry in (first, second):
        entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
        entry.build_service = lambda capabilities: seen.append(capabilities) or object()
    monkeypatch.setattr(catalog, "entries", lambda: (first, second))
    catalog.validate_business_contracts()
    outbox, ecs = object(), object()
    runtime = build_runtime(outbox=outbox, ecs_commands=ecs)
    assert set(runtime.services) == {"first", "second"}
    assert seen[0] is seen[1]
    assert seen[0].ecs is ecs
    assert seen[0].wms.outbox is outbox
    assert seen[0].transport.outbox is outbox
    assert callable(seen[0].wms.get_result)
    assert callable(seen[0].transport.get_result)
    assert callable(seen[0].transport.list_unclosed_predecessors)
    assert not hasattr(runtime, "session")


def test_runtime_requires_validated_catalog_and_available_capabilities(monkeypatch):
    from app.businesses.runtime import build_runtime

    entry = business_composition()
    entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    with pytest.raises(RuntimeError, match="validat"):
        build_runtime(outbox=object(), ecs_commands=None)
    catalog.validate_business_contracts()
    with pytest.raises(ValueError, match="outbox"):
        build_runtime(outbox=None, ecs_commands=None)


def test_empty_catalog_needs_no_communication_resources(monkeypatch):
    from app.businesses.runtime import build_runtime

    monkeypatch.setattr(catalog, "entries", lambda: ())
    catalog.validate_business_contracts()
    runtime = build_runtime(outbox=None, ecs_commands=None)
    assert runtime.services == runtime.definitions == {}


def test_business_factory_must_return_a_service(monkeypatch):
    from app.businesses.runtime import build_runtime

    entry = business_composition()
    entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
    entry.build_service = lambda capabilities: None
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    catalog.validate_business_contracts()
    with pytest.raises(ValueError, match="test_sorting.*factory must return a service"):
        build_runtime(outbox=object(), ecs_commands=None)


def test_declared_commands_require_ecs_before_any_factory(monkeypatch):
    from pydantic import BaseModel

    from app.businesses.runtime import build_runtime
    from app.integrations.ecs_command import CommandBinding, DeviceAnnex
    from app.integrations.ecs_command.contracts import ErrorDetail

    class Params(BaseModel):
        pass

    entry = business_composition()
    entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
    entry.COMMAND_BINDINGS = (CommandBinding("TEST_SCAN", "MOVE", "test", "1", Params),)
    entry.ANNEXES = {("test", "1"): DeviceAnnex(Params, Params, ErrorDetail, 0, 10)}
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    catalog.validate_business_contracts()
    with pytest.raises(ValueError, match="ECS capability"):
        build_runtime(outbox=object(), ecs_commands=None)


def test_require_runtime_accepts_only_real_runtime_container():
    from app.businesses.runtime import BusinessRuntime, require_runtime

    runtime = BusinessRuntime(definitions={}, services={"test": object()})
    assert require_runtime(SimpleNamespace(runtime=runtime)) is runtime
    for value in (None, {}, SimpleNamespace(services={})):
        with pytest.raises(RuntimeError):
            require_runtime(SimpleNamespace(runtime=value))
