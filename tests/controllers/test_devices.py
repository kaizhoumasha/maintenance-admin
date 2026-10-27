import pytest

from scripts.seed_admin import INITIAL_PERMISSIONS
from tests.helpers import bearer, make_user_with_perms


@pytest.mark.parametrize("path", ["/devices", "/work-lines"])
async def test_asset_management_requires_login(client, path):
    assert (await client.get("/api/v1" + path)).status_code == 401


async def test_admin_can_manage_empty_workline(client, admin, workline_business_catalog):
    created = await client.post(
        "/api/v1/work-lines",
        headers=admin,
        json={"line_code": "L1", "line_name": "分拣线", "business_code": "test_sorting"},
    )
    assert created.status_code == 201, created.text
    row = created.json()
    updated = await client.put(
        f"/api/v1/work-lines/{row['id']}",
        headers=admin,
        json={"version": row["version"], "line_name": "新名称"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert (
        await client.delete(f"/api/v1/work-lines/{row['id']}?version=1", headers=admin)
    ).status_code == 409
    assert (
        await client.delete(f"/api/v1/work-lines/{row['id']}?version=2", headers=admin)
    ).status_code == 204


async def test_asset_grants_do_not_make_an_administrator(client, admin):
    user = await make_user_with_perms(
        client, admin, [p for p in INITIAL_PERMISSIONS if p.startswith("asset:")]
    )
    for method, path, body in [
        ("GET", "/devices", None),
        ("GET", "/devices/1", None),
        ("PUT", "/devices/1", {}),
        ("DELETE", "/devices/1?version=1", None),
        ("POST", "/devices/assignments", {}),
        ("POST", "/devices/discovery/preview", {"ecs_origin": "http://127.0.0.1:1"}),
        ("POST", "/devices/discovery/register", {}),
        ("POST", "/devices/1/route-migrations", {}),
        ("POST", "/devices/1/refresh", None),
        ("GET", "/work-lines", None),
        ("POST", "/work-lines", {}),
        ("GET", "/work-lines/1", None),
        ("PUT", "/work-lines/1", {}),
        ("DELETE", "/work-lines/1?version=1", None),
    ]:
        response = await client.request(method, "/api/v1" + path, headers=bearer(user), json=body)
        assert response.status_code == 403, (path, response.text)
    routes = (await client.get("/api/v1/users/me/menus", headers=bearer(user))).json()
    assert all(route["name"] != "Assets" for route in routes)


async def test_admin_membership_does_not_bypass_operation_permission(client, admin):
    from sqlalchemy import delete, select

    from app.core.database import get_session
    from app.main import app
    from app.models import Permission, Role, role_permission

    async for session in app.dependency_overrides[get_session]():
        role = await session.scalar(select(Role.id).where(Role.name == "admin"))
        perm = await session.scalar(
            select(Permission.id).where(Permission.code == "asset:device:read")
        )
        await session.execute(
            delete(role_permission).where(
                role_permission.c.role_id == role, role_permission.c.permission_id == perm
            )
        )
        await session.commit()
    response = await client.get("/api/v1/devices", headers=admin)
    assert response.status_code == 403
    assert response.json()["detail"] == "asset:device:read"


async def test_revoking_admin_role_blocks_next_asset_request(client, admin):
    from sqlalchemy import delete

    from app.core.database import get_session
    from app.main import app
    from app.models import user_role

    assert (await client.get("/api/v1/devices", headers=admin)).status_code == 200
    async for session in app.dependency_overrides[get_session]():
        await session.execute(delete(user_role).where(user_role.c.user_id == 1))
        await session.commit()
    response = await client.get("/api/v1/devices", headers=admin)
    assert response.status_code == 403 and response.json()["detail"] == "ADMIN_ONLY"


async def test_every_asset_operation_requires_its_permission_even_for_admin(client, admin):
    from sqlalchemy import delete, select

    from app.core.database import get_session
    from app.main import app
    from app.models import Permission, Role, role_permission

    async for session in app.dependency_overrides[get_session]():
        role_id = await session.scalar(select(Role.id).where(Role.name == "admin"))
        codes = select(Permission.id).where(Permission.code.like("asset:%"))
        await session.execute(
            delete(role_permission).where(
                role_permission.c.role_id == role_id, role_permission.c.permission_id.in_(codes)
            )
        )
        await session.commit()
    for method, path, action in [
        ("GET", "/devices", "asset:device:read"),
        ("GET", "/devices/1", "asset:device:read"),
        ("PUT", "/devices/1", "asset:device:update"),
        ("DELETE", "/devices/1?version=1", "asset:device:delete"),
        ("POST", "/devices/assignments", "asset:device:assign"),
        ("POST", "/devices/discovery/preview", "asset:device:discover"),
        ("POST", "/devices/discovery/register", "asset:device:register"),
        ("POST", "/devices/1/route-migrations", "asset:device:migrate"),
        ("POST", "/devices/1/refresh", "asset:device:refresh"),
        ("GET", "/work-lines", "asset:workline:read"),
        ("GET", "/work-lines/1", "asset:workline:read"),
        ("POST", "/work-lines", "asset:workline:create"),
        ("PUT", "/work-lines/1", "asset:workline:update"),
        ("DELETE", "/work-lines/1?version=1", "asset:workline:delete"),
    ]:
        result = await client.request(method, "/api/v1" + path, headers=admin, json={})
        assert result.status_code == 403, (path, result.text)
        assert result.json()["detail"] == action


async def test_configuration_reads_never_query_ecs(client, admin, monkeypatch):
    from app.core.identifiers import uuid7
    from app.main import app
    from app.models import Device, DeviceRoute
    from app.services.device_status_service import DeviceStatusService

    factory = app.state.audit_session_factory
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin="http://127.0.0.1:1")
        session.add(route)
        await session.flush()
        device = Device(device_code="D1", display_name="configured", current_route_id=route.id)
        session.add(device)
        await session.flush()
        device_id = device.id

    async def forbidden(*args, **kwargs):
        raise AssertionError("configuration reads must not query ECS")

    monkeypatch.setattr(DeviceStatusService, "refresh_device", forbidden)
    expected = {
        "id",
        "device_code",
        "display_name",
        "work_line_id",
        "description",
        "sort_order",
        "current_route_id",
        "version",
        "created_at",
        "updated_at",
        "ecs_origin",
    }
    detail = await client.get(f"/api/v1/devices/{device_id}", headers=admin)
    listed = await client.get("/api/v1/devices", headers=admin)
    assert detail.status_code == listed.status_code == 200
    assert set(detail.json()) == expected
    assert listed.json()["items"] == [detail.json()]


@pytest.mark.parametrize("field", ["status", "freshness"])
async def test_runtime_filters_are_rejected(client, admin, field):
    import json

    response = await client.get(
        "/api/v1/devices",
        headers=admin,
        params={
            "filter": json.dumps({"conditions": [{"field": field, "op": "eq", "value": "OFFLINE"}]})
        },
    )
    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "outcome,expected", [("empty", 200), ("not_found", 502), ("timeout", 504), ("limited", 429)]
)
async def test_detail_status_uses_existing_refresh_endpoint(
    client, admin, real_redis, monkeypatch, outcome, expected
):
    from datetime import UTC, datetime

    from app.core.identifiers import uuid7
    from app.integrations.ecs_status import StatusQueryError, StatusQueryResult
    from app.main import app
    from app.models import Device, DeviceRoute
    from app.services.device_status_service import DeviceStatusService

    factory = app.state.audit_session_factory
    origin = "http://127.0.0.1:45904"
    async with factory.begin() as session:
        route = DeviceRoute(id=str(uuid7()), device_code="D1", ecs_origin=origin)
        session.add(route)
        await session.flush()
        device = Device(device_code="D1", display_name="configured", current_route_id=route.id)
        session.add(device)
        await session.flush()
        device_id = device.id
    calls = []

    async def query(origin, **kwargs):
        calls.append((origin, kwargs))
        if outcome == "empty":
            return StatusQueryResult(
                ecs_origin=origin,
                requested_device_code="D1",
                fetched_at=datetime.now(UTC),
                devices=(),
            )
        raise StatusQueryError(
            ecs_origin=origin,
            requested_device_code="D1",
            completed_at=datetime.now(UTC),
            error_kind="READ_TIMEOUT"
            if outcome == "timeout"
            else "NOT_FOUND"
            if outcome == "not_found"
            else "HTTP_ERROR",
            http_status=404 if outcome == "not_found" else 429 if outcome == "limited" else None,
        )

    service = DeviceStatusService(factory, real_redis, query_fn=query)
    monkeypatch.setattr(app.state, "device_status", service, raising=False)
    try:
        before = await client.get(f"/api/v1/devices/{device_id}", headers=admin)
        response = await client.post(f"/api/v1/devices/{device_id}/refresh", headers=admin)
        assert response.status_code == expected, response.text
        assert calls == [(origin, {"device_code": "D1"})]
        if outcome == "empty":
            assert response.json()["result"]["devices"] == []
            assert set(response.json()) == {
                "device_id",
                "device_version",
                "current_route_id",
                "result",
            }
        if outcome == "limited":
            assert int(response.headers["Retry-After"]) >= 29
        after = await client.get(f"/api/v1/devices/{device_id}", headers=admin)
        assert before.status_code == after.status_code == 200
        assert before.json() == after.json()
        assert len(calls) == 1
    finally:
        await real_redis.delete(service.origin_key(origin))


async def test_read_permission_does_not_authorize_status_refresh(client, admin):
    from sqlalchemy import delete, select

    from app.core.database import get_session
    from app.main import app
    from app.models import Permission, Role, role_permission

    async def forbidden(*args, **kwargs):
        raise AssertionError("read permission must not invoke the ECS query service")

    from app.controllers.asset_dependencies import device_status

    app.dependency_overrides[device_status] = forbidden
    async for session in app.dependency_overrides[get_session]():
        role = await session.scalar(select(Role.id).where(Role.name == "admin"))
        permission = await session.scalar(
            select(Permission.id).where(Permission.code == "asset:device:refresh")
        )
        await session.execute(
            delete(role_permission).where(
                role_permission.c.role_id == role, role_permission.c.permission_id == permission
            )
        )
        await session.commit()
    assert (await client.get("/api/v1/devices", headers=admin)).status_code == 200
    denied = await client.post("/api/v1/devices/1/refresh", headers=admin)
    assert denied.status_code == 403
    assert denied.json()["detail"] == "asset:device:refresh"
