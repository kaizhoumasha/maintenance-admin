import json

import pytest
from sqlalchemy import delete, select

from app.main import app
from app.models import AuditLog, Permission, Role, WorkLine, role_permission
from tests.helpers import bearer, make_user_with_perms


@pytest.fixture(autouse=True)
def deployed_catalog(workline_business_catalog):
    pass


async def test_legacy_null_line_remains_readable(client, admin):
    async with app.state.audit_session_factory.begin() as session:
        row = WorkLine(line_code="OLD", line_name="旧工作线")
        session.add(row)
        await session.flush()
        line_id = row.id
    read = await client.get(f"/api/v1/work-lines/{line_id}", headers=admin)
    assert read.status_code == 200, read.text
    assert read.json()["business_code"] is None
    changed = await client.put(
        f"/api/v1/work-lines/{line_id}",
        headers=admin,
        json={"line_name": "旧线改名", "version": 1},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["business_code"] is None
    selected = await client.put(
        f"/api/v1/work-lines/{line_id}",
        headers=admin,
        json={"line_name": "旧线改名", "version": 2, "business_code": "test_sorting"},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["business_code"] == "test_sorting"


@pytest.mark.parametrize("selection", [{}, {"business_code": None}, {"business_code": "missing"}])
async def test_new_line_requires_known_business(client, admin, selection):
    response = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json={"line_code": "NEW", "line_name": "新线", **selection},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_REQUEST"


async def test_business_can_only_be_selected_once(client, admin):
    response = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json={"line_code": "NEW", "line_name": "新线", "business_code": "test_sorting"},
    )
    assert response.status_code == 201, response.text
    line = response.json()
    for value, expected in [("test_sorting", 200), ("other_sorting", 409), (None, 409)]:
        response = await client.put(
            f"/api/v1/work-lines/{line['id']}",
            headers=admin,
            json={"line_name": "新线", "version": line["version"], "business_code": value},
        )
        assert response.status_code == expected, response.text
        if expected == 200:
            line = response.json()
    unchanged = await client.get(f"/api/v1/work-lines/{line['id']}", headers=admin)
    assert unchanged.json()["version"] == line["version"]
    assert unchanged.json()["business_code"] == "test_sorting"


async def test_business_update_omission_preserves_selection(client, admin):
    created = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json={"line_code": "NEW", "line_name": "新线", "business_code": "test_sorting"},
    )
    assert created.status_code == 201, created.text
    line = created.json()
    stale = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={"line_name": "改名", "version": 99},
    )
    assert stale.status_code == 409
    updated = await client.put(
        f"/api/v1/work-lines/{line['id']}",
        headers=admin,
        json={"line_name": "改名", "version": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["business_code"] == "test_sorting"
    assert updated.json()["version"] == 2


async def test_workline_business_is_audited_and_filterable(client, admin):
    async with app.state.audit_session_factory.begin() as session:
        old = WorkLine(line_code="OLD", line_name="旧线")
        session.add(old)
        await session.flush()
        line_id = old.id
    updated = await client.put(
        f"/api/v1/work-lines/{line_id}",
        headers=admin,
        json={
            "line_name": "旧线",
            "version": 1,
            "business_code": "test_sorting",
            "position_bindings": {"INLET": "P1"},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    for op, value, count in [
        ("eq", "test_sorting", 1),
        ("in", ["other_sorting"], 0),
        ("is_null", None, 0),
    ]:
        condition = {"field": "business_code", "op": op}
        if value is not None:
            condition["value"] = value
        response = await client.get(
            "/api/v1/work-lines",
            headers=admin,
            params={"filter": json.dumps({"conditions": [condition]})},
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] == count
    async with app.state.audit_session_factory() as session:
        logs = list(
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "asset:workline:update", AuditLog.resource_id == line_id
                )
            )
        )
        assert len(logs) == 1
        assert logs[0].detail["before"]["business_code"] is None
        assert logs[0].detail["after"]["business_code"] == "test_sorting"
        assert logs[0].detail["after"]["position_bindings"] == {"INLET": "P1"}


async def test_business_options_require_admin_and_workline_read(client, admin):
    assert (await client.get("/api/v1/businesses")).status_code == 401
    regular = await make_user_with_perms(client, admin, ["asset:workline:read"])
    assert (await client.get("/api/v1/businesses", headers=bearer(regular))).status_code == 403
    result = await client.get("/api/v1/businesses", headers=admin)
    assert result.status_code == 200, result.text
    first = result.json()[0]
    assert set(first) == {
        "code",
        "display_name",
        "device_roles",
        "position_slots",
        "initial_entries",
    }
    assert first["device_roles"][0] == {"key": "SCAN1", "display_name": "扫码", "required": True}
    assert first["position_slots"] == [{"key": "INLET", "display_name": "入口", "required": True}]
    assert first["initial_entries"] == [
        {"kind": "ecs_event", "name": "SCAN_COMPLETED", "device_role": "SCAN1"}
    ]
    async with app.state.audit_session_factory.begin() as session:
        role_id = await session.scalar(select(Role.id).where(Role.name == "admin"))
        permission = await session.scalar(
            select(Permission.id).where(Permission.code == "asset:workline:read")
        )
        await session.execute(
            delete(role_permission).where(
                role_permission.c.role_id == role_id, role_permission.c.permission_id == permission
            )
        )
    denied = await client.get("/api/v1/businesses", headers=admin)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "asset:workline:read"


async def test_created_business_selection_is_in_transaction_audit(client, admin):
    response = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json={"line_code": "NEW", "line_name": "新线", "business_code": "test_sorting"},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["version"] == 1
    async with app.state.audit_session_factory() as session:
        logs = list(
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "asset:workline:create", AuditLog.resource_id == row["id"]
                )
            )
        )
        assert len(logs) == 1
        assert logs[0].detail["before"] == {}
        assert logs[0].detail["after"]["business_code"] == "test_sorting"
