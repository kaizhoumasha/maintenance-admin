"""Coordinate trusted formal and local imports using the existing three registries."""

from typing import Literal

from app.businesses import catalog
from app.integrations.ecs_callback.discovery import candidate_modules as ecs_candidates
from app.integrations.registration import (
    DEV_MODULES,
    formal_registration,
    import_registration_module,
)
from app.integrations.wms_callback.discovery import candidate_modules as callback_candidates
from app.integrations.wms_request.discovery import candidate_modules as request_candidates

Kind = Literal["ecs_event", "callback", "request"]
_kinds: tuple[Kind, ...] = ("ecs_event", "callback", "request")
_loaded_formal: set[Kind] = set()
_loading = False


def _candidates(kinds: tuple[Kind, ...], environment: str) -> dict[str, bool]:
    candidates = {}
    for kind in kinds:
        builtin = {
            "ecs_event": ecs_candidates,
            "callback": callback_candidates,
            "request": request_candidates,
        }[kind]
        candidates.update((name, False) for name in builtin(environment))
        candidates.update(
            (name, True) for name in catalog.entry_modules(kind, environment=environment)
        )
    return candidates


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
            for name, required in candidates.items():
                import_registration_module(name, required=required)
        _loaded_formal.update(kinds)
    finally:
        _loading = False


def load_local_modules(*, environment: str, kind: Kind | None = None) -> None:
    if environment != "local":
        return
    load_formal_modules()
    for name, required in _candidates(_kinds if kind is None else (kind,), environment).items():
        if name.split(".")[3] in DEV_MODULES:
            import_registration_module(name, local_only=True, required=required)
