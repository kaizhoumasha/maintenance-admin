from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.models import AuditLog, IntegrationExchange


async def test_history_routes_require_authentication(client):
    for path in (
        "/integrations/exchanges",
        f"/integrations/exchanges/{uuid7()}",
        f"/integrations/exchanges/{uuid7()}/records",
        f"/integrations/records/{uuid7()}/body",
        f"/integrations/diagnostics?peer=WMS&reference={uuid7()}",
        f"/integrations/http-exchanges/WMS/{uuid7()}/request",
    ):
        assert (await client.get("/api/v1" + path)).status_code == 401


async def test_history_permission_guards_every_metadata_route(client, admin):
    from app.main import app
    from app.models import Permission, Role, role_permission

    async with app.state.audit_session_factory.begin() as session:
        permission_id = await session.scalar(
            select(Permission.id).where(Permission.code == "integration:history:read")
        )
        role_id = await session.scalar(select(Role.id).where(Role.name == "admin"))
        assert permission_id is not None and role_id is not None
        await session.execute(
            role_permission.delete().where(
                role_permission.c.role_id == role_id,
                role_permission.c.permission_id == permission_id,
            )
        )
    for path in (
        "/integrations/exchanges",
        f"/integrations/exchanges/{uuid7()}",
        f"/integrations/exchanges/{uuid7()}/records",
        f"/integrations/diagnostics?peer=WMS&reference={uuid7()}",
    ):
        response = await client.get("/api/v1" + path, headers=admin)
        assert response.status_code == 403, response.text


async def test_payload_permission_and_metadata_only_audit(client, admin, monkeypatch, caplog):
    from app.integrations.history import RecordDraft, write_record
    from app.main import app
    from app.models import Permission, Role, role_permission

    factory = app.state.audit_session_factory
    monkeypatch.setattr(app.state, "integration_history_session_factory", factory, raising=False)
    async with factory.begin() as session:
        root = IntegrationExchange(
            role="DIAGNOSTIC",
            protocol="ecs-event",
            direction="INBOUND",
            interaction_kind="EVENT",
            identity_kind="DIAGNOSTIC",
            identity_value="test",
            created_at=datetime.now(UTC),
        )
        session.add(root)
        await session.flush()
        record_id = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=root.id,
                record_kind="CALLBACK_RECEIVED",
                event_key="private",
                direction="INBOUND",
                raw_body=b"PRIVATE_BODY",
            ),
        )
        permission = await session.scalar(
            select(Permission).where(Permission.code == "integration:payload:read")
        )
        assert permission is not None
        role_id = await session.scalar(select(Role.id).where(Role.name == "admin"))
        await session.execute(
            role_permission.delete().where(
                role_permission.c.role_id == role_id,
                role_permission.c.permission_id == permission.id,
            )
        )
    assert (await client.get("/api/v1/integrations/exchanges", headers=admin)).status_code == 200
    path = f"/api/v1/integrations/records/{record_id}/body"
    assert (await client.get(path, headers=admin)).status_code == 403
    async with factory.begin() as session:
        await session.execute(
            role_permission.insert().values(role_id=role_id, permission_id=permission.id)
        )
    answer = await client.get(path, headers=admin)
    assert answer.status_code == 200 and answer.content == b"PRIVATE_BODY"
    assert answer.headers["content-type"] == "application/octet-stream"
    assert answer.headers["x-integration-body-state"] == "FULL"
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "integration:payload:read")
            )
        )
        assert len(rows) == 1 and rows[0].resource_id is None
        assert rows[0].detail["record_id"] == str(record_id)
        assert "body" not in rows[0].detail and "PRIVATE_BODY" not in str(rows[0].detail)

    def failed_audit():
        raise RuntimeError("PRIVATE_AUDIT_FAILURE")

    monkeypatch.setattr(app.state, "audit_session_factory", failed_audit)
    unchanged = await client.get(path, headers=admin)
    assert unchanged.status_code == 200 and unchanged.content == b"PRIVATE_BODY"
    assert "PRIVATE_AUDIT_FAILURE" not in caplog.text and "PRIVATE_BODY" not in caplog.text
    async with factory.begin() as session:
        await session.execute(
            role_permission.delete().where(
                role_permission.c.role_id == role_id,
                role_permission.c.permission_id == permission.id,
            )
        )
    assert (await client.get(path, headers=admin)).status_code == 403


async def test_history_detail_reads_one_independent_read_only_snapshot(
    client, admin, callback_db, monkeypatch
):
    from sqlalchemy import text, update

    from app.integrations import history as records
    from app.main import app
    from app.models import ReliableMessage
    from app.reliable import MessageDraft, Outbox
    from app.reliable.config import TargetConfig
    from app.services import integration_history_service as history
    from tests.helpers import OpaqueHistoryAdapter

    if callback_db.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL read-only repeatable-read snapshot")
    monkeypatch.setattr(app.state, "integration_history_session_factory", callback_db)
    outbox = Outbox(
        {"history-rr": TargetConfig("history-rr", "http://example.test", "opaque")},
        {"opaque": OpaqueHistoryAdapter()},
        records=records,
    )
    async with callback_db.begin() as session:
        message = await outbox.enqueue(
            session, MessageDraft("history-rr", "opaque", str(uuid7()), b"frozen")
        )
        root_id = (await session.get(ReliableMessage, message.message_id)).exchange_id
    original = history._summaries
    changed = False

    async def during_snapshot(session, rows):
        nonlocal changed
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        if not changed:
            async with callback_db.begin() as writer:
                await writer.execute(
                    update(IntegrationExchange)
                    .where(IntegrationExchange.id == root_id)
                    .values(coverage="PARTIAL")
                )
                await writer.execute(
                    update(ReliableMessage)
                    .where(ReliableMessage.id == message.message_id)
                    .values(status="DONE")
                )
            changed = True
        return await original(session, rows)

    monkeypatch.setattr(history, "_summaries", during_snapshot)
    path = f"/api/v1/integrations/exchanges/{root_id}"
    first = await client.get(path, headers=admin)
    assert first.status_code == 200, first.text
    assert first.json()["coverage"] == "DECLARED" and first.json()["delivery"]["state"] == "PENDING"
    second = await client.get(path, headers=admin)
    assert second.status_code == 200
    assert second.json()["coverage"] == "PARTIAL" and second.json()["delivery"]["state"] == "DONE"


async def test_diagnostics_finds_unmatched_ecs_callback_and_keeps_wire_out_of_summary(
    client, admin, callback_db, monkeypatch
):
    from types import SimpleNamespace

    from app.main import app

    monkeypatch.setattr(app.state, "integration_history_session_factory", callback_db)
    monkeypatch.setattr(app.state, "ecs_commands", SimpleNamespace(), raising=False)
    payload = {
        "command_code": str(uuid7()),
        "device_code": "D1",
        "result": "SUCCESS",
        "finish_time": 1,
        "data": {"note": "private-example"},
    }
    callback = await client.post("/api/v1/callback/result", json=payload)
    assert callback.status_code == 404
    response = await client.get(
        "/api/v1/integrations/diagnostics",
        headers=admin,
        params={"peer": "ECS", "reference": payload["command_code"]},
    )
    assert response.status_code == 200
    assert "private-example" not in response.text
    result = response.json()
    assert result["runtime"] == []
    [row] = result["items"]
    assert row["http_status"] == 404 and row["reason_code"] == "COMMAND_NOT_FOUND"
    assert row["initiator"] == "PEER" and "direction" not in row
    wire = await client.get(
        f"/api/v1/integrations/http-exchanges/ECS/{row['id']}/response", headers=admin
    )
    assert wire.content == callback.content
    data = await client.get(
        f"/api/v1/integrations/http-exchanges/ECS/{row['id']}/request",
        headers=admin,
        params={"representation": "payload"},
    )
    assert data.status_code == 200 and data.json() == payload["data"]
    async with app.state.audit_session_factory() as session:
        audits = (
            await session.scalars(select(AuditLog).where(AuditLog.resource == "http_exchange"))
        ).all()
    assert {
        (entry.detail["side"], entry.detail["representation"])
        for entry in audits
        if entry.detail["record_id"] == row["id"]
    } == {("request", "payload"), ("response", "wire")}
    assert "private-example" not in str([entry.detail for entry in audits])
