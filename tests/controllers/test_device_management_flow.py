import json
from functools import partial

from fastapi import FastAPI
from sqlalchemy import event, func, select

from app.core.redis import get_redis
from app.integrations.ecs_status import query
from app.main import app
from app.models import AuditLog, Device, DeviceRoute, ReliableMessage
from app.services.device_status_service import DeviceStatusService
from tests.helpers import ecs_status_document, loopback_app, simulator_client_factory


async def test_preview_register_migrate_delete_reregister_with_two_local_ecs(
    client, admin, real_redis, monkeypatch, workline_business_catalog
):
    mock = FastAPI()
    calls = []

    @mock.get("/api/v1/device/status")
    async def status(device_code: str | None = None):
        calls.append(device_code)
        return ecs_status_document(*([device_code] if device_code else ["D1", "D2"]))

    scheduled = []

    def schedule(self, args=None, **kwargs):
        scheduled.extend(args or [])

    monkeypatch.setattr("celery.app.task.Task.apply_async", schedule)
    app.dependency_overrides[get_redis] = lambda: real_redis
    factory = app.state.audit_session_factory
    tokens = []
    async with factory() as session:
        engine = session.get_bind()
    writes = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    async with loopback_app(mock) as first, loopback_app(mock) as second:
        service = DeviceStatusService(
            factory,
            real_redis,
            query_fn=partial(query, client_factory=simulator_client_factory([first, second])),
        )
        monkeypatch.setattr(app.state, "device_status", service, raising=False)

        async def preview(origin):
            response = await client.post(
                "/api/v1/devices/discovery/preview", headers=admin, json={"ecs_origin": origin}
            )
            assert response.status_code == 200, response.text
            tokens.append(response.json()["preview_token"])
            return response.json()

        try:
            initial = await preview(first)
            assert {item["classification"] for item in initial["items"]} == {"NEW"}
            response = await client.post(
                "/api/v1/devices/discovery/register",
                headers=admin,
                json={"preview_token": initial["preview_token"], "device_codes": ["D1"]},
            )
            assert response.status_code == 201, response.text
            old = response.json()[0]
            assert scheduled == []
            assert not any("device_observations" in sql for sql in writes)
            assert not {"observation", "freshness", "time_anomaly"} & old.keys()
            assert calls == [None]
            await real_redis.hset(service.origin_key(first), "next_at", 0)
            refreshed = await client.post(f"/api/v1/devices/{old['id']}/refresh", headers=admin)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["result"]["devices"][0]["state"]["status"] == "OFFLINE"
            assert refreshed.json()["device_id"] == old["id"]
            assert refreshed.json()["device_version"] == old["version"]
            line = (
                await client.post(
                    "/api/v1/work-lines",
                    headers=admin,
                    json={"line_code": "L1", "line_name": "线1", "business_code": "test_sorting"},
                )
            ).json()
            assigned = await client.post(
                "/api/v1/devices/assignments",
                headers=admin,
                json={
                    "items": [{"device_id": old["id"], "version": 1}],
                    "work_line_id": line["id"],
                },
            )
            assert assigned.status_code == 200, assigned.text
            current = assigned.json()[0]
            assert (
                await client.delete(f"/api/v1/work-lines/{line['id']}?version=1", headers=admin)
            ).status_code == 409
            before_calls = len(calls)
            filt = json.dumps(
                {"conditions": [{"field": "work_line_id", "op": "eq", "value": line["id"]}]}
            )
            listed = await client.get("/api/v1/devices", headers=admin, params={"filter": filt})
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 1 and len(calls) == before_calls
            conflict = await preview(second)
            assert conflict["items"][0]["classification"] == "ADDRESS_CONFLICT"
            invalid = await client.post(
                f"/api/v1/devices/{old['id']}/route-migrations",
                headers=admin,
                json={
                    "version": current["version"],
                    "expected_origin": "not-an-origin",
                    "preview_token": conflict["preview_token"],
                    "reason": "纠正",
                    "confirmed": True,
                },
            )
            assert invalid.status_code == 400, invalid.text
            response = await client.post(
                f"/api/v1/devices/{old['id']}/route-migrations",
                headers=admin,
                json={
                    "version": current["version"],
                    "expected_origin": first,
                    "preview_token": conflict["preview_token"],
                    "reason": "配置纠正",
                    "confirmed": True,
                },
            )
            assert response.status_code == 200, response.text
            migrated = response.json()
            assert (
                migrated["ecs_origin"] == second
                and migrated["current_route_id"] != old["current_route_id"]
            )
            assert (
                await client.delete(
                    f"/api/v1/devices/{old['id']}?version={migrated['version']}", headers=admin
                )
            ).status_code == 204
            assert (
                await client.get(f"/api/v1/devices/{old['id']}", headers=admin)
            ).status_code == 404
            assert (
                await client.delete(f"/api/v1/work-lines/{line['id']}?version=1", headers=admin)
            ).status_code == 204
            replay = await client.post(
                "/api/v1/devices/discovery/register",
                headers=admin,
                json={"preview_token": initial["preview_token"], "device_codes": ["D1"]},
            )
            assert replay.status_code == 409, replay.text
            await real_redis.hset(service.origin_key(first), "next_at", 0)
            fresh = await preview(first)
            new = (
                await client.post(
                    "/api/v1/devices/discovery/register",
                    headers=admin,
                    json={"preview_token": fresh["preview_token"], "device_codes": ["D1"]},
                )
            ).json()[0]
            assert new["id"] != old["id"] and new["current_route_id"] != old["current_route_id"]
            for method, path, body in [
                ("PUT", f"/devices/{old['id']}", {"version": 1, "display_name": "old ID write"}),
                ("DELETE", f"/devices/{old['id']}?version=1", None),
                ("POST", f"/devices/{old['id']}/refresh", None),
                (
                    "POST",
                    "/devices/assignments",
                    {"items": [{"device_id": old["id"], "version": 1}], "work_line_id": None},
                ),
                (
                    "POST",
                    f"/devices/{old['id']}/route-migrations",
                    {
                        "version": 1,
                        "expected_origin": first,
                        "preview_token": conflict["preview_token"],
                        "reason": "old ID migration",
                        "confirmed": True,
                    },
                ),
            ]:
                denied = await client.request(method, "/api/v1" + path, headers=admin, json=body)
                assert denied.status_code == 404, (path, denied.text)
            current = (await client.get(f"/api/v1/devices/{new['id']}", headers=admin)).json()
            assert (
                current["version"] == new["version"]
                and current["display_name"] == new["display_name"]
            )
            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(Device)) == 1
                assert await session.scalar(select(func.count()).select_from(DeviceRoute)) == 3
                assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0
                deleted = list(
                    await session.scalars(
                        select(AuditLog).where(AuditLog.action == "asset:device:delete")
                    )
                )
                assert len(deleted) == 1 and deleted[0].detail["before"]["ecs_origin"] == second
            assert calls == [None, "D1", None, None]
            assert scheduled == []
            assert not any("device_observations" in sql for sql in writes)
        finally:
            event.remove(engine, "before_cursor_execute", observe)
            await real_redis.delete(
                *(service.origin_key(o) for o in [first, second]),
                *("device-preview:" + t for t in tokens),
            )
