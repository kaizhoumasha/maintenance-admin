"""Explicit deployed business catalog; listing reads metadata without building services."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Literal

from app.operation_names import is_operation_name

from .definitions import BusinessDefinition

if TYPE_CHECKING:
    from app.integrations.ecs_command import CommandBinding, DeviceAnnex

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
    from app.integrations.ecs_command import CommandBinding, DeviceAnnex
    from app.integrations.ecs_command.contracts import merge_annexes
    from app.integrations.registration import DEV_MODULES
    from app.integrations.wms_callback.registry import operations

    global _validated_entries
    _validated_entries = None
    current = entries()
    definitions()  # Preserve definition/code/operation validation for every composition.
    commands, annexes, owners = [], {}, {}
    registered_events, callbacks = events(), operations()
    for entry in current:
        definition = entry.DEFINITION
        prefix = entry.__name__.rsplit(".", 1)[0] + "."
        roles = {role.role_key for role in definition.device_roles}
        declared = entry.ANNEXES
        location = f"{definition.code} ({entry.__name__})"

        def own_formal(item, prefix=prefix):
            source = item.registration_source
            return (
                not source.local_only
                and source.module.startswith(prefix)
                and source.module.removeprefix(prefix).split(".")[0] not in DEV_MODULES
            )

        for command in entry.COMMAND_BINDINGS:
            if not isinstance(command, CommandBinding):
                raise ValueError(f"{location}: CommandBinding required")
            source = command.registration_source.location
            if command.device_role not in roles:
                raise ValueError(
                    f"{location}: undeclared command role {command.device_role!r}: {source}"
                )
            key = (command.annex_id, command.annex_version)
            if not isinstance(declared.get(key), DeviceAnnex):
                raise ValueError(
                    f"{location}: command requires own declared annex {key!r}: {source}"
                )
            command_key = (command.device_role, command.task_type)
            if command_key in owners:
                raise ValueError(
                    f"duplicate command {command_key!r}: {owners[command_key]} "
                    f"and {location}: {source}"
                )
            owners[command_key] = f"{location}: {source}"
            commands.append(command)
        annexes = merge_annexes(annexes, declared, source=location)
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
    _validated_entries = current
    return tuple(commands), annexes
