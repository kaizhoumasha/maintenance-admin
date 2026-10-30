import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.models import AuditLog, WorkLine
from app.schemas.workline import WorkLineCreate, WorkLineUpdate
from app.services import workline_service as service
from app.services.asset_audit import AssetActor
from tests.helpers import add_bound_device, workline_definition_lookup

ACTOR = AssetActor(1, "business-test", None, None)


async def test_unknown_business_uses_invalid_request_and_rolls_back(db):
    with pytest.raises(AppError) as error:
        await service.create_workline(
            db,
            WorkLineCreate(line_code="L1", line_name="Line", business_code="missing"),
            actor=ACTOR,
            definition_lookup=workline_definition_lookup,
        )
    assert error.value.code == "INVALID_REQUEST"
    assert await db.scalar(select(WorkLine.id)) is None
    assert await db.scalar(select(AuditLog.id)) is None


async def test_partial_bindings_can_save_but_wrong_keys_cannot(db):
    line, device, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1")
    line.device_bindings = {}
    line.position_bindings = {}
    await db.commit()
    line_id = line.id
    selected = await service.update_workline(
        db,
        line_id,
        WorkLineUpdate(line_name="Line", version=1, business_code="test_sorting"),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    assert selected.device_bindings == selected.position_bindings == {}
    for updates in [
        {"device_bindings": {"UNKNOWN": "D1"}},
        {"position_bindings": {"UNKNOWN": "P1"}},
    ]:
        with pytest.raises(AppError) as error:
            await service.update_workline(
                db,
                line_id,
                WorkLineUpdate(line_name="Line", version=2, **updates),
                actor=ACTOR,
                definition_lookup=workline_definition_lookup,
            )
        assert error.value.code == "INVALID_REQUEST"
        assert (await service.read_workline(db, line_id)).version == 2
    saved = await service.update_workline(
        db,
        line_id,
        WorkLineUpdate(
            line_name="Line",
            version=2,
            device_bindings={"SCAN1": "D1"},
            position_bindings={"INLET": "just-a-string"},
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    assert saved.version == 3


async def test_selecting_business_rechecks_existing_bindings_atomically(db):
    line, _, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1", role="OLD_ROLE")
    await db.commit()
    line_id = line.id
    with pytest.raises(AppError) as error:
        await service.update_workline(
            db,
            line_id,
            WorkLineUpdate(line_name="Changed", version=1, business_code="test_sorting"),
            actor=ACTOR,
            definition_lookup=workline_definition_lookup,
        )
    assert error.value.code == "INVALID_REQUEST"
    current = await service.read_workline(db, line_id)
    assert current.business_code is None and current.version == 1
    assert current.line_name == "Test line"
    assert await db.scalar(select(AuditLog.id)) is None
    changed = await service.update_workline(
        db,
        line_id,
        WorkLineUpdate(
            line_name="Changed",
            version=1,
            business_code="test_sorting",
            device_bindings={"SCAN1": "D1"},
        ),
        actor=ACTOR,
        definition_lookup=workline_definition_lookup,
    )
    assert changed.business_code == "test_sorting" and changed.version == 2


async def test_cross_line_or_duplicate_device_is_rejected(db):
    line, _, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1")
    other, _, _ = await add_bound_device(db, "D2", "http://127.0.0.1:1")
    await db.commit()
    with pytest.raises(AppError) as error:
        await service.update_workline(
            db,
            line.id,
            WorkLineUpdate(
                line_name="Line",
                version=1,
                business_code="test_sorting",
                device_bindings={"SCAN1": "D2"},
            ),
            actor=ACTOR,
            definition_lookup=workline_definition_lookup,
        )
    assert error.value.code == "CONFLICT"
    with pytest.raises(ValueError):
        WorkLineUpdate(line_name="Line", version=1, device_bindings={"SCAN1": "D1", "SCAN2": "D1"})


async def test_configuration_issue_checks_locked_line_and_current_definition(db):
    from app.services.device_bindings import resolve_binding

    line, _, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1")
    await db.commit()

    async def issue(code="test_sorting", lookup=workline_definition_lookup):
        await db.flush()
        binding = await resolve_binding(db, "D1")
        assert binding is not None
        return await service.business_configuration_issue(
            db, binding, code, definition_lookup=lookup
        )

    assert await issue() == "WORKLINE_BUSINESS_MISMATCH"
    line.business_code = "other_sorting"
    assert await issue() == "WORKLINE_BUSINESS_MISMATCH"
    line.business_code = "test_sorting"
    assert await issue() is None
    line.position_bindings = {}
    assert await issue() == "WORKLINE_CONFIGURATION_INCOMPLETE"
    line.position_bindings = {"INLET": "unverified-position"}
    line.device_bindings = {"SCAN2": "D1"}
    assert await issue() == "WORKLINE_CONFIGURATION_INCOMPLETE"
    line.device_bindings = {"UNKNOWN": "D1"}
    assert await issue() == "WORKLINE_CONFIGURATION_INCOMPLETE"
    line.device_bindings = {"SCAN1": "D1"}
    assert await issue("missing") == "WORKLINE_BUSINESS_MISMATCH"

    def unavailable(code):
        raise LookupError(code)

    assert await issue(lookup=unavailable) == "WORKLINE_CONFIGURATION_INCOMPLETE"

    def broken(code):
        raise RuntimeError("catalog failed")

    with pytest.raises(RuntimeError, match="catalog failed"):
        await issue(lookup=broken)


@pytest.mark.parametrize("change", ["version", "bindings", "delete"])
async def test_configuration_issue_does_not_hide_binding_races(db, change):
    from app.services.device_bindings import resolve_binding

    line, _, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1")
    line.business_code = "test_sorting"
    await db.commit()
    binding = await resolve_binding(db, "D1")
    if change == "version":
        line.version += 1
    elif change == "bindings":
        line.position_bindings = {"INLET": "changed"}
    else:
        await db.delete(line)
    await db.flush()
    with pytest.raises(AppError) as error:
        await service.business_configuration_issue(
            db, binding, "test_sorting", definition_lookup=workline_definition_lookup
        )
    assert error.value.code == "CONFLICT"


async def test_configuration_issue_propagates_database_failure(db, monkeypatch):
    from app.services.device_bindings import resolve_binding

    line, _, _ = await add_bound_device(db, "D1", "http://127.0.0.1:1")
    line.business_code = "test_sorting"
    await db.commit()
    binding = await resolve_binding(db, "D1")

    async def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "get", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.business_configuration_issue(
            db, binding, "test_sorting", definition_lookup=workline_definition_lookup
        )


async def test_operation_resource_subset_preserves_business_and_binding_guards(db):
    from app.businesses.definitions import BusinessDefinition, DeviceRole, PositionSlot
    from app.services.device_bindings import resolve_binding

    line, _, _ = await add_bound_device(db, "D3", "http://127.0.0.1:1", role="SCAN3")
    line.business_code = "manual_test"
    line.position_bindings = {}
    await db.commit()
    definition = BusinessDefinition(
        code="manual_test",
        display_name="Manual",
        device_roles=(
            DeviceRole(role_key="SCAN1", display_name="Input"),
            DeviceRole(role_key="SCAN3", display_name="Return"),
        ),
        position_slots=(PositionSlot(slot_key="OUTLET", display_name="Outlet"),),
    )
    binding = await resolve_binding(db, "D3")

    async def issue(**kwargs):
        return await service.business_configuration_issue(
            db, binding, "manual_test", definition_lookup=lambda _: definition, **kwargs
        )

    assert await issue() == "WORKLINE_CONFIGURATION_INCOMPLETE"
    assert await issue(required_device_roles=("SCAN3",), required_position_slots=()) is None
    with pytest.raises(ValueError, match="declared"):
        await issue(required_device_roles=("UNDECLARED",), required_position_slots=())
    line.business_code = "other"
    await db.flush()
    assert await issue(required_device_roles=("SCAN3",), required_position_slots=()) == (
        "WORKLINE_BUSINESS_MISMATCH"
    )


@pytest.mark.parametrize("case", ["allowed", "undeclared", "other_line", "missing_device"])
async def test_new_command_requires_declared_contract_and_own_workline(db, monkeypatch, case):
    from dataclasses import replace

    from pydantic import BaseModel, ConfigDict

    from app.businesses import catalog
    from app.businesses.definitions import DeviceRole
    from app.integrations.ecs_callback import registry as events
    from app.integrations.ecs_command import CommandOwner, ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_request import operation as requests
    from app.services.device_routes import authorize_new_command
    from tests.helpers import business_composition

    class Result(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        qty: int

    @ecs_command(
        "MOVE",
        device_roles=("SCAN1",),
        annex_id="auth",
        annex_version="1",
        result=Result,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )
    class Move(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        qty: int

    first, second = business_composition("first"), business_composition("second")
    for entry in (first, second):
        entry.DEFINITION = replace(
            entry.DEFINITION,
            initial_entries=(),
            device_roles=(DeviceRole(role_key="SCAN1", display_name="Scan"),),
        )
        entry.ENTRY_MODULES = ()
    first.COMMANDS = (Move,)
    monkeypatch.setattr(catalog, "entries", lambda: (first, second))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(events, "_registry", {})
    monkeypatch.setattr(callbacks, "_registry", {})
    monkeypatch.setattr(requests, "_registry", {})
    catalog.validate_business_contracts()
    line, device, _ = await add_bound_device(db, "AUTH-1", "http://127.0.0.1:1")
    line.business_code = "first" if case in {"allowed", "missing_device"} else "second"
    await db.flush()
    owner = CommandOwner(domain="second" if case == "undeclared" else "first")
    device_code = "unknown" if case == "missing_device" else device.device_code
    if case == "allowed":
        await authorize_new_command(db, owner, device_code, Move)
    else:
        with pytest.raises(ValueError, match="configured|authorized"):
            await authorize_new_command(db, owner, device_code, Move)
