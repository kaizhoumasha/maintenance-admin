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
