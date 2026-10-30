from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from app.businesses import catalog
from app.businesses.definitions import DeviceRole, InitialEntry, PositionSlot
from app.integrations.ecs_callback import registry as events
from app.integrations.registration import RegistrationSource, validate_registration_file
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_request import operation as requests
from tests.helpers import business_composition


@pytest.fixture
def shared_component(monkeypatch):
    from app.businesses.components import ComponentDefinition

    component = ComponentDefinition(
        key="shared_scan",
        device_roles=(DeviceRole(role_key="SHARED_SCAN", display_name="Shared scan"),),
        position_slots=(PositionSlot(slot_key="OUTLET", display_name="Outlet"),),
        initial_entries=(
            InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="SHARED_SCAN"),
        ),
        entry_modules=("app.businesses._components.shared_scan.ecs_event.scan_completed",),
        model_modules=("app.businesses._components.shared_scan.models",),
    )
    first, second = business_composition("first"), business_composition("second")
    for entry in (first, second):
        entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
        entry.ENTRY_MODULES = ()
        entry.COMPONENTS = (component,)
    monkeypatch.setattr(catalog, "entries", lambda: (first, second))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(events, "_registry", {})
    monkeypatch.setattr(callbacks, "_registry", {})
    monkeypatch.setattr(requests, "_registry", {})
    return SimpleNamespace(component=component, first=first, second=second)


def test_shared_component_resources_and_modules_are_merged_once(shared_component, monkeypatch):
    component = shared_component.component
    definitions = catalog.definitions()
    for definition in definitions:
        assert definition.device_roles[-1] == component.device_roles[0]
        assert definition.position_slots == component.position_slots
        assert definition.initial_entries == component.initial_entries
    assert catalog.entry_modules("ecs_event", environment="production") == component.entry_modules
    loaded = []
    monkeypatch.setattr(catalog, "import_module", loaded.append)
    catalog.load_business_models()
    assert loaded == list(component.model_modules)
    assert {d.code for d in definitions} == {"first", "second"}
    with pytest.raises(FrozenInstanceError):
        component.key = "other"


@pytest.mark.parametrize("problem", ["duplicate", "nested", "resource", "input", "module"])
def test_invalid_component_composition_fails_before_runtime(shared_component, problem):
    state = shared_component
    if problem == "duplicate":
        state.first.COMPONENTS += (state.component,)
    elif problem == "nested":
        state.first.COMPONENTS = (SimpleNamespace(COMPONENTS=(state.component,)),)
    elif problem == "resource":
        state.first.DEFINITION = replace(
            state.first.DEFINITION,
            position_slots=(PositionSlot(slot_key="OUTLET", display_name="Conflicting"),),
        )
    elif problem == "input":
        state.first.DEFINITION = replace(
            state.first.DEFINITION,
            device_roles=state.component.device_roles,
            initial_entries=state.component.initial_entries,
        )
    else:
        state.first.COMPONENTS = (
            replace(state.component, entry_modules=("app.businesses.other.ecs_event",)),
        )
    with pytest.raises(ValueError):
        catalog.definitions()
        catalog.entry_modules("ecs_event", environment="production")


def test_shared_event_keeps_source_and_business_routes(shared_component):
    async def handler(ctx, payload):
        pass

    source = RegistrationSource(
        shared_component.component.entry_modules[0], "scan_completed.py", "handler", False
    )
    events._registry[("SHARED_SCAN", "SCAN_COMPLETED")] = events.Registered(handler, None, source)
    catalog.validate_business_contracts()
    key = ("ecs_event", "SCAN_COMPLETED", "SHARED_SCAN")
    assert catalog.contract_consumers(key) == frozenset({"first", "second"})
    for business in ("first", "second"):
        target = catalog.input_target(business, key)
        assert target.component_key == "shared_scan"
        assert target.source == source
    assert catalog.input_target("unrelated", key) is None
    assert catalog.progression_owner(source) is None


def test_component_change_invalidates_validated_catalog(shared_component):
    for entry in (shared_component.first, shared_component.second):
        entry.COMPONENTS = ()
    catalog.validate_business_contracts()
    shared_component.first.COMPONENTS = (shared_component.component,)
    with pytest.raises(RuntimeError):
        catalog.validated_entries()


@pytest.mark.parametrize("local", [False, True])
def test_shared_source_still_enforces_filename_and_formal_scope(
    shared_component, monkeypatch, local
):
    from app.integrations.registration import local_registration

    async def wrong(ctx, payload):
        pass

    wrong.__module__ = "app.businesses._components.shared_scan.ecs_event.wrong"
    monkeypatch.setattr(catalog, "_registration_guard_enabled", False)
    if local:
        with local_registration(), pytest.raises(RuntimeError, match="首次注册"):
            validate_registration_file("SCAN_COMPLETED", wrong)
    else:
        with pytest.raises(ValueError, match="filename mismatch"):
            validate_registration_file("SCAN_COMPLETED", wrong)


def test_shared_command_compiles_once_for_the_same_role(shared_component):
    from pydantic import BaseModel, ConfigDict

    from app.integrations.ecs_command import ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail

    class Data(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")

    @ecs_command(
        "MOVE",
        device_roles=("SHARED_SCAN",),
        annex_id="shared-move",
        annex_version="1",
        result=Data,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=100,
    )
    class Move(Data):
        pass

    component = replace(
        shared_component.component, initial_entries=(), entry_modules=(), commands=(Move,)
    )
    shared_component.first.COMPONENTS = shared_component.second.COMPONENTS = (component,)
    bindings, annexes = catalog.validate_business_contracts()
    assert len(bindings) == 1
    assert bindings[0].source is Move
    assert len(annexes) == 1
    assert catalog.contract_consumers(("ecs_command", "MOVE", "SHARED_SCAN")) == {"first", "second"}


@pytest.mark.parametrize("problem", [None, "missing", "method", "progression", "extra"])
async def test_business_composes_multiple_internal_components(shared_component, problem):
    from types import MappingProxyType

    from app.businesses.components import ComponentDefinition
    from app.businesses.runtime import build_runtime

    component = replace(
        shared_component.component,
        initial_entries=(),
        entry_modules=(),
        requires_progression=True,
        required_methods=("accept", "scan_pending"),
    )
    second = ComponentDefinition(key="another", required_methods=("scan_pending",))
    visited = []

    class Internal:
        def __init__(self, remaining):
            self.remaining = remaining

        async def accept(self, ctx, payload):
            pass

        async def scan_pending(self):
            visited.append(self)
            count = min(5, self.remaining)
            self.remaining -= count
            return count

    for entry in (shared_component.first, shared_component.second):
        entry.COMPONENTS = (component, second)
        entry.PROGRESSION = "scan_pending"

        def factory(capabilities):
            parts = {"shared_scan": Internal(7), "another": Internal(2)}
            if problem == "missing":
                parts.pop("shared_scan")
            elif problem == "method":
                parts["shared_scan"].accept = None
            elif problem == "extra":
                parts["undeclared"] = Internal(1)

            async def scan_pending():
                return sum([await part.scan_pending() for part in parts.values()])

            return SimpleNamespace(components=MappingProxyType(parts), scan_pending=scan_pending)

        entry.build_service = factory
    if problem == "progression":
        del shared_component.first.PROGRESSION
    if problem:
        with pytest.raises(ValueError):
            catalog.validate_business_contracts()
            build_runtime(outbox=object(), ecs_commands=None, session_factory=lambda: None)
    else:
        catalog.validate_business_contracts()
        runtime = build_runtime(outbox=object(), ecs_commands=None, session_factory=lambda: None)
        assert set(runtime.services) == set(runtime.progressions) == {"first", "second"}
        assert await runtime.progressions["first"]() == 7
        assert [part.remaining for part in visited] == [2, 0]
        assert len(visited) == 2
        assert not any(
            v is runtime.services["second"].components[k]
            for v in visited
            for k in ("shared_scan", "another")
        )


def test_shared_namespace_does_not_grant_undeclared_entry_modules(shared_component):
    async def handler(ctx, payload):
        pass

    component = shared_component.component
    shared_component.second.COMPONENTS = (replace(component, initial_entries=(), entry_modules=()),)
    source = RegistrationSource(component.entry_modules[0], "scan_completed.py", "scan", False)
    events._registry[("SHARED_SCAN", "SCAN_COMPLETED")] = events.Registered(handler, None, source)
    catalog.validate_business_contracts()
    key = ("ecs_event", "SCAN_COMPLETED", "SHARED_SCAN")
    assert catalog.contract_consumers(key) == {"first"}
    assert catalog.input_target("second", key) is None


def test_shared_component_local_entries_use_existing_local_import_guard(
    shared_component, monkeypatch
):
    from app.integrations import discovery

    component = replace(
        shared_component.component,
        entry_modules=shared_component.component.entry_modules
        + ("app.businesses._components.shared_scan.dev_ecs_event",),
    )
    shared_component.first.COMPONENTS = shared_component.second.COMPONENTS = (component,)
    imported = []
    monkeypatch.setattr(discovery, "_loaded_formal", set(discovery._kinds))
    monkeypatch.setattr(
        discovery,
        "import_registration_module",
        lambda *args, **kwargs: imported.append((args, kwargs)),
    )
    discovery.load_local_modules(environment="local", kind="ecs_event")
    component_imports = [item for item in imported if "_components" in item[0][0]]
    assert component_imports == [
        (
            ("app.businesses._components.shared_scan.dev_ecs_event",),
            {"local_only": True, "required": True},
        )
    ]
    imported.clear()
    discovery.load_local_modules(environment="production", kind="ecs_event")
    assert imported == []


def test_unreferenced_component_cannot_register_after_validation(shared_component):
    from app.integrations.discovery import validate_late_registration

    for entry in (shared_component.first, shared_component.second):
        entry.COMPONENTS = (replace(shared_component.component, initial_entries=()),)
    catalog.validate_business_contracts()
    validate_late_registration(
        RegistrationSource(
            shared_component.component.entry_modules[0], "scan_completed.py", "scan", False
        )
    )
    with pytest.raises(ValueError):
        validate_late_registration(
            RegistrationSource(
                "app.businesses._components.unused.ecs_event.scan_completed",
                "scan_completed.py",
                "scan",
                False,
            )
        )


def test_component_callback_operation_name_is_validated_in_effective_definition(shared_component):
    for entry in (shared_component.first, shared_component.second):
        entry.COMPONENTS = (
            replace(
                shared_component.component,
                initial_entries=(InitialEntry(kind="wms_callback", name="invalid"),),
            ),
        )
    with pytest.raises(ValueError, match="invalid WMS operation"):
        catalog.definitions()


@pytest.mark.parametrize("dev_entry", ["dev", "dev_callback", "dev_ecs_event"])
def test_component_dev_commands_cannot_enter_formal_compilation(dev_entry):
    from pydantic import BaseModel, ConfigDict

    from app.integrations.ecs_command import compile_commands, ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail

    class Result(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        qty: int

    class Move(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        qty: int

    Move.__module__ = f"app.businesses._components.shared_scan.{dev_entry}"
    ecs_command(
        "MOVE",
        device_roles=("SHARED_SCAN",),
        annex_id="local",
        annex_version="1",
        result=Result,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )(Move)
    with pytest.raises(ValueError, match="local-only command"):
        compile_commands(Move)


@pytest.mark.parametrize(
    "values",
    [
        {"entry_modules": ["app.businesses._components.shared_scan.ecs_event"]},
        {"model_modules": ("same", "same")},
        {"required_methods": ("invalid method",)},
        {"commands": (object(),)},
        {"requires_progression": 1},
    ],
)
def test_invalid_component_declarations_are_rejected(values):
    from app.businesses.components import ComponentDefinition

    with pytest.raises(ValueError):
        ComponentDefinition(key="shared_scan", **values)


@pytest.mark.parametrize("problem", ["business", "target", "assembly"])
def test_component_lookup_requires_its_business_target_and_assembly(problem):
    from app.businesses.runtime import BusinessRuntime, require_component

    part = object()
    service = SimpleNamespace(components={"shared_scan": part})
    context = SimpleNamespace(
        runtime=BusinessRuntime(definitions={}, services={"first": service}),
        business_code=None if problem == "business" else "first",
        component_key="other" if problem == "target" else "shared_scan",
    )
    if problem == "assembly":
        service.components = {}
    with pytest.raises(RuntimeError):
        require_component(context, "shared_scan")


def test_component_models_cannot_escape_the_declared_component_namespace(shared_component):
    shared_component.first.COMPONENTS = (
        replace(shared_component.component, model_modules=("app.businesses.other.models",)),
    )
    with pytest.raises(ValueError, match="component MODEL_MODULES path"):
        catalog.load_business_models()
