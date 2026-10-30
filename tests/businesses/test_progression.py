"""Only declared formal owners can schedule a business-owned async pending entry."""

import pytest


@pytest.fixture
def progression_catalog():
    from app.businesses import catalog

    with pytest.MonkeyPatch.context() as monkeypatch:
        yield monkeypatch
    catalog.validate_business_contracts()


@pytest.mark.parametrize("declaration", [123, "", "scan.pending"])
def test_invalid_progression_declaration_fails_before_runtime(progression_catalog, declaration):
    from app.businesses import catalog
    from app.businesses.coarse_sorting import composition

    progression_catalog.setattr(composition, "PROGRESSION", declaration, raising=False)
    with pytest.raises(ValueError, match="PROGRESSION"):
        catalog.validate_business_contracts()


async def test_declared_pending_entry_is_bound_to_its_business(coarse_forward):
    flow = coarse_forward
    assert await flow.runtime.progressions["coarse_sorting"]() == 0


def test_registered_sources_route_only_to_declared_formal_progressions():
    from app.businesses import catalog
    from app.integrations.registration import RegistrationSource
    from app.integrations.wms_request.operation import operations

    source = operations()["inbound.coarse_sorting.validate_bind@v1"].registration_source
    assert catalog.progression_owner(source) == "coarse_sorting"
    assert (
        catalog.progression_owner(
            RegistrationSource("app.integrations.wms_request.dev", "dev.py", "Demo", True)
        )
        is None
    )


async def test_non_async_pending_entry_is_rejected(coarse_forward, progression_catalog):
    from app.businesses import catalog
    from app.businesses.runtime import build_runtime

    progression_catalog.setattr(catalog.entries()[0], "PROGRESSION", "read_scan", raising=False)
    with pytest.raises(ValueError, match="PROGRESSION"):
        build_runtime(
            outbox=coarse_forward.outbox,
            ecs_commands=coarse_forward.commands,
            session_factory=coarse_forward.factory,
        )


async def test_shared_task_isolates_business_pending_work(coarse_forward, monkeypatch):
    from dataclasses import replace
    from types import ModuleType

    from app.businesses import catalog
    from app.core import get_settings
    from app.tasks import business_tasks as tasks

    flow = coarse_forward
    entries = catalog.entries()
    other = ModuleType("app.businesses.other.composition")
    other.DEFINITION = replace(
        entries[0].DEFINITION, code="other", device_roles=(), position_slots=(), initial_entries=()
    )
    other.ENTRY_MODULES, other.MODEL_MODULES, other.COMMANDS = (), (), ()
    other.PROGRESSION = "scan_pending"
    other_pending = {"other-instance"}

    class OtherService:
        async def scan_pending(self):
            count = len(other_pending)
            other_pending.clear()
            return count

    other.build_service = lambda _: OtherService()
    with monkeypatch.context() as scope:
        scope.setattr(catalog, "entries", lambda: (*entries, other))
        catalog.validate_business_contracts()
        settings = get_settings().model_copy(
            update={
                "RELIABLE_TARGETS": {
                    "targets": {
                        "coarse-wms": {
                            "base_url": flow.outbox.targets["coarse-wms"].base_url,
                            "protocol": "wms-decisions",
                        }
                    }
                }
            }
        )
        scope.setattr(tasks, "get_settings", lambda: settings)
        assert (await flow.post(flow.event())).status_code == 200
        assert await tasks.advance_pending(flow.factory, "other") == 1
        assert (await flow.rows())[0]["validation_message_id"] is None
        assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
        assert (await flow.rows())[0]["validation_message_id"] is not None
        assert await tasks.advance_pending(flow.factory, "other") == 0
    catalog.validate_business_contracts()


@pytest.mark.parametrize("kind", ["positional", "keyword_only"])
async def test_pending_entry_requiring_arguments_is_rejected_at_assembly(
    coarse_forward, progression_catalog, kind
):
    from app.businesses import catalog
    from app.businesses.runtime import build_runtime

    class Positional:
        async def scan_pending(self, required):
            return 0

    class KeywordOnly:
        async def scan_pending(self, *, required):
            return 0

    service = Positional() if kind == "positional" else KeywordOnly()
    progression_catalog.setattr(catalog.entries()[0], "build_service", lambda _: service)
    with pytest.raises(ValueError, match="PROGRESSION"):
        build_runtime(
            outbox=coarse_forward.outbox,
            ecs_commands=coarse_forward.commands,
            session_factory=coarse_forward.factory,
        )
