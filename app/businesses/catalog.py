"""Explicit deployed business catalog; listing reads metadata without building services."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Literal

from app.operation_names import is_operation_name

from .definitions import BusinessDefinition

if TYPE_CHECKING:
    from app.integrations.ecs_command.contracts import CommandBinding, DeviceAnnex

_validated_entries: tuple[ModuleType, ...] | None = None


def entries() -> tuple[ModuleType, ...]:
    """Import actual composition modules here when a complete business is deployed."""
    return ()


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
        for initial in definition.initial_entries:
            if initial.kind == "wms_callback" and not is_operation_name(initial.name):
                raise ValueError(f"{definition.code}: invalid WMS operation {initial.name!r}")
        result.append(definition)
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
        owner = entry.__name__.rsplit(".", 1)[0].split(".")
        for name in entry.ENTRY_MODULES:
            parts = name.split(".") if isinstance(name, str) else []
            if (
                len(parts) < 4
                or parts[:3] != owner
                or parts[:2] != ["app", "businesses"]
                or parts[3] not in supported
                or not all(part.isidentifier() for part in parts)
            ):
                raise ValueError(f"{entry.__name__}: invalid owned ENTRY_MODULES path {name!r}")
            if parts[3] in suffixes:
                selected.append(name)
    return tuple(selected)


def load_business_models() -> None:
    """Load all managed models regardless of workline configuration or running services."""
    for entry in entries():
        for name in getattr(entry, "MODEL_MODULES", ()):
            import_module(name)


def validated_entries() -> tuple[ModuleType, ...]:
    current = entries()
    if _validated_entries is None or current != _validated_entries:
        raise RuntimeError("Business catalog must be validated before runtime assembly")
    return current


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

    global _validated_entries
    _validated_entries = None
    current = entries()
    all_roles = {role.role_key for definition in definitions() for role in definition.device_roles}
    commands, annexes, owners, annex_sources = [], {}, {}, {}
    registered_events, callbacks = events(), operations()
    for entry in current:
        definition = entry.DEFINITION
        prefix = entry.__name__.rsplit(".", 1)[0] + "."
        roles = {role.role_key for role in definition.device_roles}
        compiled, declared = compile_commands(*entry.COMMANDS)
        location = f"{definition.code} ({entry.__name__})"
        for model in entry.COMMANDS:
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
            if command_key in owners:
                raise ValueError(
                    f"duplicate command {command_key!r}: {owners[command_key]} "
                    f"and {location}: {source}; 修复: 每个角色/任务只声明一次"
                )
            owners[command_key] = f"{location}: {source}"
            entry_annex_sources[key] = f"{location}: {source}"
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
            if item is None or not own_formal(item):
                source = item.registration_source.location if item else "missing registration"
                raise ValueError(
                    f"{location}: initial entry {key!r} requires own formal registration: {source}"
                )
    formal_modules = tuple(
        name
        for kind in ("ecs_event", "callback", "request")
        for name in entry_modules(kind, environment="production")
    )
    for item in (*registered_events.values(), *callbacks.values(), *request_operations().values()):
        source = item.registration_source
        if source.local_only or not source.module.startswith("app.businesses."):
            continue
        if not any(
            source.module == name or source.module.startswith(name + ".") for name in formal_modules
        ):
            raise ValueError(
                f"{source.location}: registration requires a catalog-owned formal ENTRY_MODULES "
                "path; 修复: 将完整业务列入 catalog，并显式声明所属正式入口"
            )
    _validated_entries = current
    return tuple(commands), annexes
