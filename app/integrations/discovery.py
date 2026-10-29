"""Coordinate trusted formal and local imports using the existing three registries."""

from typing import Literal

from app.businesses import catalog
from app.integrations.registration import (
    DEV_MODULES,
    PLATFORM_FORMAL_MODULES,
    PLATFORM_LOCAL_MODULES,
    RegistrationSource,
    formal_registration,
    import_registration_module,
)

Kind = Literal["ecs_event", "callback", "request"]
_kinds: tuple[Kind, ...] = ("ecs_event", "callback", "request")
_loaded_formal: set[Kind] = set()
_loading = False


def validate_late_registration(source: RegistrationSource) -> None:
    """Keep actual registration's catalog access in the existing integration bridge."""
    catalog.validate_registration_owners((source,), allow_unvalidated=True)


def _candidates(kinds: tuple[Kind, ...], environment: str) -> tuple[str, ...]:
    candidates = []
    for kind in kinds:
        candidates.extend(PLATFORM_FORMAL_MODULES[kind])
        if environment == "local":
            candidates.extend(PLATFORM_LOCAL_MODULES[kind])
        candidates.extend(catalog.entry_modules(kind, environment=environment))
    return tuple(dict.fromkeys(candidates))


def load_formal_modules(kind: Kind | None = None) -> None:
    global _loading
    if _loading:
        raise RuntimeError("reentrant formal discovery: entry imports must not query the catalog")
    kinds = tuple(k for k in (_kinds if kind is None else (kind,)) if k not in _loaded_formal)
    if not kinds:
        return
    _loading = True
    try:
        candidates = _candidates(kinds, "production")
        with formal_registration():
            for name in candidates:
                import_registration_module(name, required=True)
        _loaded_formal.update(kinds)
    finally:
        _loading = False


def load_local_modules(*, environment: str, kind: Kind | None = None) -> None:
    if environment != "local":
        return
    load_formal_modules()
    for name in _candidates(_kinds if kind is None else (kind,), environment):
        if name.split(".")[3] in DEV_MODULES:
            import_registration_module(name, local_only=True, required=True)
