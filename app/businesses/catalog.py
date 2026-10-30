"""Explicit deployed business catalog; listing reads metadata without building services."""

from dataclasses import dataclass, replace
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Literal

from app.operation_names import is_operation_name

from .components import ComponentDefinition
from .definitions import BusinessDefinition

if TYPE_CHECKING:
    from app.integrations.ecs_command.contracts import CommandBinding, DeviceAnnex
    from app.integrations.registration import RegistrationSource

_validated_entries: "_CatalogSnapshot | None" = None
# Failed revalidation invalidates runtime assembly without reopening startup registration.
_registration_guard_enabled = False


@dataclass(frozen=True)
class InputTarget:
    component_key: str | None
    source: "RegistrationSource"


@dataclass(frozen=True)
class _CatalogSnapshot:
    entries: tuple[ModuleType, ...]
    metadata: tuple
    consumers: dict[tuple[str, str, str | None], frozenset[str]]
    targets: dict[tuple[str, tuple[str, str, str | None]], InputTarget]
    commands: dict[tuple[str, str], type]


def components(entry) -> tuple[ComponentDefinition, ...]:
    values = getattr(entry, "COMPONENTS", ())
    if not isinstance(values, tuple) or any(not isinstance(v, ComponentDefinition) for v in values):
        raise ValueError(f"{entry.__name__}: COMPONENTS must be a tuple of ComponentDefinition")
    if len({v.key for v in values}) != len(values):
        raise ValueError(f"{entry.__name__}: duplicate component key")
    return values


def _providers(entry):
    yield (
        None,
        entry.__name__.rsplit(".", 1)[0],
        getattr(entry, "DEFINITION", None),
        getattr(entry, "ENTRY_MODULES", ()),
        getattr(entry, "MODEL_MODULES", ()),
        getattr(entry, "COMMANDS", ()),
    )
    for component in components(entry):
        yield (
            component.key,
            f"app.businesses._components.{component.key}",
            component,
            component.entry_modules,
            component.model_modules,
            component.commands,
        )


def _snapshot():
    return tuple(
        (
            entry,
            entry.DEFINITION,
            tuple(entry.ENTRY_MODULES),
            tuple(entry.COMMANDS),
            tuple(getattr(entry, "MODEL_MODULES", ())),
            components(entry),
            getattr(entry, "PROGRESSION", None),
        )
        for entry in entries()
    )


def _effective(entry):
    if not components(entry):
        return entry.DEFINITION
    combined = {}
    for field, key in (
        ("device_roles", "role_key"),
        ("position_slots", "slot_key"),
        ("initial_entries", None),
    ):
        values = {}
        for _, _, definition, _, _, _ in _providers(entry):
            for value in getattr(definition, field):
                identity = getattr(value, key) if key else value
                if identity in values and (key is None or values[identity] != value):
                    raise ValueError(f"{entry.DEFINITION.code}: conflicting {field}: {identity!r}")
                values[identity] = value
        combined[field] = tuple(values.values())
    for component in components(entry):
        if component.requires_progression and getattr(entry, "PROGRESSION", None) is None:
            raise ValueError(f"{entry.__name__}: component requires business PROGRESSION")
    return replace(entry.DEFINITION, **combined)


def effective_definition(business_code: str) -> BusinessDefinition:
    return get_definition(business_code)


def contract_consumers(key: tuple[str, str, str | None]) -> frozenset[str]:
    validated_entries()
    return _validated_entries.consumers.get(key, frozenset())


def input_target(business_code: str, key: tuple[str, str, str | None]) -> InputTarget | None:
    validated_entries()
    return _validated_entries.targets.get((business_code, key))


def command_is_allowed(business_code: str, role: str, model: type, task_type: str) -> bool:
    return (
        business_code in contract_consumers(("ecs_command", task_type, role))
        and _validated_entries.commands.get((role, task_type)) is model
    )


def entries() -> tuple[ModuleType, ...]:
    """Explicit business compositions shared by API, worker and model loading."""
    from .coarse_sorting import composition

    return (composition,)


def definitions() -> tuple[BusinessDefinition, ...]:
    result = []
    codes = set()
    for entry in entries():
        definition = entry.DEFINITION
        if not isinstance(definition, BusinessDefinition):
            raise ValueError(f"{entry.__name__}: DEFINITION must be a BusinessDefinition")
        if definition.code in codes:
            raise ValueError(f"duplicate business code: {definition.code}")
        codes.add(definition.code)
        progression = getattr(entry, "PROGRESSION", None)
        if progression is not None and (
            not isinstance(progression, str) or not progression.isidentifier()
        ):
            raise ValueError(f"{entry.__name__}: PROGRESSION must name an async service method")
        effective = _effective(entry)
        for initial in effective.initial_entries:
            if initial.kind == "wms_callback" and not is_operation_name(initial.name):
                raise ValueError(f"{definition.code}: invalid WMS operation {initial.name!r}")
        result.append(effective)
    return tuple(result)


def get_definition(code: str) -> BusinessDefinition:
    for definition in definitions():
        if definition.code == code:
            return definition
    raise LookupError(f"unknown business code: {code!r}")


def entry_modules(
    kind: Literal["ecs_event", "callback", "request"], *, environment: str
) -> tuple[str, ...]:
    """Return trusted fixed entry paths without importing handlers or services."""
    suffixes = {kind}
    if environment == "local":
        suffixes.add(
            {"ecs_event": "dev_ecs_event", "callback": "dev_callback", "request": "dev"}[kind]
        )
    supported = {"ecs_event", "callback", "request", "dev_ecs_event", "dev_callback", "dev"}
    selected = []
    for entry in entries():
        for _, prefix, _, modules, _, _ in _providers(entry):
            for name in modules:
                relative = name.removeprefix(prefix + ".") if isinstance(name, str) else ""
                parts = relative.split(".")
                if (
                    not isinstance(name, str)
                    or not name.startswith(prefix + ".")
                    or parts[0] not in supported
                    or not all(part.isidentifier() for part in name.split("."))
                ):
                    raise ValueError(f"{entry.__name__}: invalid owned ENTRY_MODULES path {name!r}")
                if parts[0] in suffixes:
                    selected.append(name)
    return tuple(dict.fromkeys(selected))


def load_business_models() -> None:
    """Load all managed models regardless of workline configuration or running services."""
    modules = []
    for entry in entries():
        for component_key, prefix, _, _, names, _ in _providers(entry):
            for name in names:
                if component_key is not None and not name.startswith(prefix + "."):
                    raise ValueError(f"{entry.__name__}: invalid component MODEL_MODULES path")
                modules.append(name)
    for name in dict.fromkeys(modules):
        import_module(name)


def validated_entries() -> tuple[ModuleType, ...]:
    current = entries()
    if (
        _validated_entries is None
        or current != _validated_entries.entries
        or _validated_entries.metadata != _snapshot()
    ):
        raise RuntimeError("Business catalog must be validated before runtime assembly")
    return current


def progressions() -> dict[str, str]:
    """Declared pending entries, shared by API routing and Beat assembly."""
    return {
        entry.DEFINITION.code: entry.PROGRESSION
        for entry in validated_entries()
        if getattr(entry, "PROGRESSION", None) is not None
    }


def progression_owner(source: "RegistrationSource") -> str | None:
    if source.local_only or not source.module.startswith("app.businesses."):
        return None
    validate_registration_owners((source,))
    for entry in validated_entries():
        if getattr(entry, "PROGRESSION", None) is not None and source.module.startswith(
            entry.__name__.rsplit(".", 1)[0] + "."
        ):
            return entry.DEFINITION.code
    return None


def validate_registration_owners(
    sources: tuple["RegistrationSource", ...], *, allow_unvalidated: bool = False
) -> None:
    """Require exact formal entry ownership; initial imports defer to the final crosscheck."""
    if allow_unvalidated and not _registration_guard_enabled:
        return
    sources = tuple(
        source
        for source in sources
        if not source.local_only and source.module.startswith("app.businesses.")
    )
    if allow_unvalidated and not sources:
        return
    formal_modules = tuple(
        name
        for kind in ("ecs_event", "callback", "request")
        for name in entry_modules(kind, environment="production")
    )
    for source in sources:
        if not any(
            source.module == name or source.module.startswith(name + ".") for name in formal_modules
        ):
            raise ValueError(
                f"{source.location}: registration requires a catalog-owned formal ENTRY_MODULES "
                "path; 修复: 将完整业务列入 catalog，并显式声明所属正式入口"
            )


def _declares_source(prefix: str, modules: tuple[str, ...], source) -> bool:
    return (
        not source.local_only
        and source.module.startswith(prefix + ".")
        and (
            not source.module.startswith("app.")
            or any(
                source.module == module or source.module.startswith(module + ".")
                for module in modules
            )
        )
    )


def validate_business_contracts() -> tuple[
    tuple["CommandBinding", ...], dict[tuple[str, str], "DeviceAnnex"]
]:
    """Crosscheck all formal registrations before local imports or resource assembly."""
    from app.integrations.ecs_callback.registry import events
    from app.integrations.ecs_command import compile_commands
    from app.integrations.ecs_command.contracts import merge_annexes
    from app.integrations.registration import DEV_MODULES
    from app.integrations.wms_callback.registry import operations
    from app.integrations.wms_request.operation import operations as request_operations

    global _validated_entries, _registration_guard_enabled
    _validated_entries = None
    current = entries()
    all_roles = {role.role_key for definition in definitions() for role in definition.device_roles}
    commands, annexes, owners, annex_sources = [], {}, {}, {}
    registered_events, callbacks = events(), operations()
    compiled_by_key = {}
    consumers, targets = {}, {}
    registrations = [
        *(("ecs_event", name, role, item) for (role, name), item in registered_events.items()),
        *(("wms_callback", name, None, item) for name, item in callbacks.items()),
    ]
    for entry in current:
        roles = {r.role_key for r in _effective(entry).device_roles}
        for component_key, prefix, _, modules, _, _ in _providers(entry):
            for kind, name, role, item in registrations:
                source = item.registration_source
                if not _declares_source(prefix, modules, source):
                    continue
                if source.module.removeprefix(prefix + ".").split(".")[0] in DEV_MODULES:
                    continue
                if kind == "ecs_event" and role not in roles:
                    raise ValueError(f"{entry.DEFINITION.code}: undeclared event role {role!r}")
                key = (kind, name, role)
                consumers.setdefault(key, set()).add(entry.DEFINITION.code)
                route = (entry.DEFINITION.code, key)
                if route in targets:
                    raise ValueError(f"{entry.DEFINITION.code}: duplicate input target {key!r}")
                targets[route] = InputTarget(component_key, source)

    for entry in current:
        definition = _effective(entry)
        prefix = entry.__name__.rsplit(".", 1)[0] + "."
        roles = {role.role_key for role in definition.device_roles}
        models = tuple(
            dict.fromkeys(
                model
                for _, _, _, _, _, declared_models in _providers(entry)
                for model in declared_models
            )
        )
        compiled, declared = compile_commands(*models)
        location = f"{definition.code} ({entry.__name__})"
        for model in models:
            model_bindings = [binding for binding in compiled if binding.source is model]
            model_roles = {binding.device_role for binding in model_bindings}
            source = model_bindings[0].registration_source.location
            if model_roles - all_roles:
                raise ValueError(
                    f"{location}: undeclared command role {model_roles - all_roles!r}: {source}"
                )
            if not model_roles & roles:
                raise ValueError(f"{location}: command requires an own declared role: {source}")
        entry_annex_sources = {}

        def own_formal(item, prefix=prefix):
            source = item.registration_source
            return (
                not source.local_only
                and source.module.startswith(prefix)
                and source.module.removeprefix(prefix).split(".")[0] not in DEV_MODULES
            )

        for command in compiled:
            if command.device_role not in roles:
                continue
            source = command.registration_source.location
            key = (command.annex_id, command.annex_version)
            command_key = (command.device_role, command.task_type)
            existing = compiled_by_key.get(command_key)
            if existing is not None and existing != command:
                raise ValueError(
                    f"duplicate command {command_key!r}: {owners[command_key]} "
                    f"and {location}: {source}; 修复: 每个角色/任务只声明一次"
                )
            consumers.setdefault(
                ("ecs_command", command.task_type, command.device_role), set()
            ).add(definition.code)
            owners.setdefault(command_key, f"{location}: {source}")
            entry_annex_sources[key] = f"{location}: {source}"
            if existing is None:
                compiled_by_key[command_key] = command
                commands.append(command)
        for key, annex in declared.items():
            source = entry_annex_sources.get(key, location)
            try:
                annexes = merge_annexes(annexes, {key: annex}, source=source)
            except ValueError as exc:
                raise ValueError(
                    f"{exc}; original source: {annex_sources.get(key, location)}; "
                    "修复: 同一附录身份保留精确原模型与超时合同"
                ) from None
            annex_sources.setdefault(key, source)
        for (role, name), item in registered_events.items():
            if own_formal(item) and role not in roles:
                raise ValueError(
                    f"{location}: undeclared event role {role!r} for {name}: "
                    f"{item.registration_source.location}"
                )
        for initial in definition.initial_entries:
            key = (
                (initial.device_role, initial.name) if initial.kind == "ecs_event" else initial.name
            )
            item = (registered_events if initial.kind == "ecs_event" else callbacks).get(key)
            contract_key = (initial.kind, initial.name, initial.device_role)
            if item is None or (definition.code, contract_key) not in targets:
                source = item.registration_source.location if item else "missing registration"
                raise ValueError(
                    f"{location}: initial entry {key!r} requires own formal registration: {source}"
                )
    for name, item in request_operations().items():
        source = item.registration_source
        if source.local_only:
            continue
        for entry in current:
            if any(
                _declares_source(prefix, modules, source)
                for _, prefix, _, modules, _, _ in _providers(entry)
            ):
                consumers.setdefault(("wms_request", name, None), set()).add(entry.DEFINITION.code)
    validate_registration_owners(
        tuple(
            item.registration_source
            for item in (
                *registered_events.values(),
                *callbacks.values(),
                *request_operations().values(),
            )
        )
    )
    _validated_entries = _CatalogSnapshot(
        current,
        _snapshot(),
        {key: frozenset(value) for key, value in consumers.items()},
        targets,
        {key: value.source for key, value in compiled_by_key.items()},
    )
    _registration_guard_enabled = True
    return tuple(commands), annexes
