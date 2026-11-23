"""Immutable declarations for code reused inside catalog-owned businesses."""

from dataclasses import dataclass

from .definitions import BusinessDefinition, DeviceRole, InitialEntry, PositionSlot


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentDefinition:
    key: str
    device_roles: tuple[DeviceRole, ...] = ()
    position_slots: tuple[PositionSlot, ...] = ()
    initial_entries: tuple[InitialEntry, ...] = ()
    entry_modules: tuple[str, ...] = ()
    model_modules: tuple[str, ...] = ()
    commands: tuple[type, ...] = ()
    required_methods: tuple[str, ...] = ()
    requires_progression: bool = False

    def __post_init__(self):
        BusinessDefinition(
            code=self.key,
            display_name=self.key,
            device_roles=self.device_roles,
            position_slots=self.position_slots,
            initial_entries=self.initial_entries,
        )
        for field in ("entry_modules", "model_modules", "required_methods"):
            values = getattr(self, field)
            if (
                not isinstance(values, tuple)
                or any(
                    not isinstance(v, str)
                    or not v
                    or not all(part.isidentifier() for part in v.split("."))
                    for v in values
                )
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"{self.key}: invalid {field}")
        if not isinstance(self.commands, tuple) or any(
            not isinstance(v, type) for v in self.commands
        ):
            raise ValueError(f"{self.key}: commands must be a tuple of models")
        if type(self.requires_progression) is not bool:
            raise ValueError(f"{self.key}: requires_progression must be bool")
