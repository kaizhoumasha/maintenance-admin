import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.integrations.runtime_authority import require_reconciled
from app.models import (
    AuditLog,
    CallbackReceipt,
    EcsHttpExchange,
    Permission,
    Role,
    TransportHttpExchange,
    WmsHttpExchange,
    role_permission,
)
from scripts.retire_http_exchange_legacy import (
    CHECKS,
    PROOFS,
    TABLES,
    WRITERS,
    RetirementRefused,
    read_evidence,
    retire,
)
from tests.helpers import run_alembic


@pytest.fixture
async def cutover(state_migration, monkeypatch):
    url = state_migration.engine.url.render_as_string(hide_password=False)
    result = await run_alembic(url, "-x", "fence_legacy_writes=true", "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr
    factory = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    await require_reconciled(factory)
    return factory


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_authority")],
    indirect=True,
)
async def test_view_migration_preserves_existing_metadata_grants(state_migration, monkeypatch):
    engine = state_migration.engine
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    with pytest.raises(RuntimeError, match="successful reconciliation"):
        await require_reconciled(async_sessionmaker(engine))
    role = "history_existing_" + uuid7().hex
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role}"))
        await connection.execute(text(f"GRANT USAGE ON SCHEMA enterprise_backend TO {role}"))
        for view in ("integration_exchange_summary", "integration_record_summary"):
            await connection.execute(text(f"GRANT SELECT ON {view} TO {role}"))
    try:
        await state_migration.migrate("upgrade", "http_exchange_query_views")
        refused = await run_alembic(
            engine.url.render_as_string(hide_password=False),
            "-x",
            "fence_legacy_writes=true",
            "upgrade",
            "head",
        )
        assert refused.returncode
        assert "fencing all legacy writers" in refused.stdout + refused.stderr
        await state_migration.migrate("downgrade", "http_exchange_runtime_authority")
        await state_migration.migrate("upgrade", "http_exchange_query_views")
        async with engine.begin() as connection:
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            for view in ("integration_exchange_summary", "integration_record_summary"):
                assert await connection.scalar(text(f"SELECT count(*) FROM {view}")) == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
async def test_query_and_body_permission_with_old_tables_hidden(
    cutover, client, admin, monkeypatch
):
    from app.main import app
    from app.models import ReliableMessage

    monkeypatch.setattr(app.state, "integration_history_session_factory", cutover)
    wms_id, ecs_id, transport_id = str(uuid7()), str(uuid7()), "transport-" + str(uuid7())
    receipt_only_id = str(uuid7())
    wire = b'{"data":{"private":"fixture-body"}}'
    payload = {"private": "fixture-body"}
    now = datetime.now(UTC)
    details = dict(
        direction="INBOUND",
        traffic_source="PEER",
        started_at=now,
        finished_at=now,
        http_method="POST",
        endpoint_path="/callbacks",
        trace_id="trace-fixture",
        request_wire=wire,
        request_wire_state="FULL",
        request_wire_sha256=sha256(wire).hexdigest(),
        request_wire_bytes=len(wire),
        request_payload=payload,
        response_wire=b"",
        response_wire_state="FULL",
        response_wire_sha256=sha256(b"").hexdigest(),
        response_wire_bytes=0,
        attempt_state="RESPONSE_PREPARED",
        http_status=422,
        reason_code="INVALID_ENVELOPE",
    )
    attempts = {
        "WMS": WmsHttpExchange(
            id=uuid7(),
            operation_id=UUID(wms_id),
            operation="test@v1",
            interaction_kind="CALLBACK",
            **details,
        ),
        "ECS": EcsHttpExchange(
            id=uuid7(), command_code=ecs_id, interaction_kind="RESULT", **details
        ),
        "TRANSPORT": TransportHttpExchange(
            id=uuid7(),
            transport_task_id=transport_id,
            interaction_kind="RESULT_CALLBACK",
            task_match_state="UNMATCHED",
            **details,
        ),
    }
    replay_id = uuid7()
    async with cutover.begin() as session:
        session.add_all(attempts.values())
        session.add_all(
            CallbackReceipt(
                receipt_id=receipt_id,
                id=uuid7(),
                operation="test@v1",
                operation_id=operation_id,
                protocol="wms-callback",
                active=True,
                raw_digest=sha256(wire).hexdigest(),
                compare_mode="RAW",
                status_code=200,
                resp_timestamp=int(now.timestamp() * 1000),
                created_at=now,
            )
            for receipt_id, operation_id in ((1201, wms_id), (1202, receipt_only_id))
        )
        attempts["WMS"].receipt_id = 1201
        session.add(
            WmsHttpExchange(
                id=replay_id,
                operation_id=UUID(wms_id),
                operation="test@v1",
                interaction_kind="CALLBACK",
                replayed_from_id=attempts["WMS"].id,
                **details,
            )
        )
        for name in TABLES:
            await session.execute(text(f"ALTER TABLE {name} RENAME TO hidden_{name}"))
        session.add(
            ReliableMessage(
                target="isolated",
                operation="fixture@v1",
                operation_id=wms_id,
                business_key=transport_id,
                status="DONE",
                exchange_id=uuid7(),
                request_record_id=uuid7(),
                frozen_request_wire=b'{"request":1}',
                terminal_response_wire=b'{"response":2}',
                last_response_wire=b'{"last":3}',
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
        )
    from app.services import dev_submit_service, dev_wms_request_service

    async with cutover() as session:
        for row in (
            (await dev_wms_request_service.message_status(session, wms_id))[0],
            (await dev_submit_service.message_status(session, transport_id))[0],
        ):
            assert (row.request, row.response, row.last_response) == (
                {"request": 1},
                {"response": 2},
                {"last": 3},
            )
    root = "/api/v1/integrations"
    for peer, reference in (("WMS", wms_id), ("ECS", ecs_id), ("TRANSPORT", transport_id)):
        result = await client.get(
            root + "/diagnostics", headers=admin, params={"peer": peer, "reference": reference}
        )
        assert result.status_code == 200, result.text
        assert str(attempts[peer].id) in {row["id"] for row in result.json()["items"]}
        assert "fixture-body" not in result.text
        if peer == "WMS":
            assert result.json()["runtime"][0]["receipt_id"] == 1201
        params = {"peer_system": peer, "identity_value": reference, "limit": 1}
        listed = await client.get(root + "/exchanges", headers=admin, params=params)
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][0]
        if peer == "WMS":
            next_page = await client.get(
                root + "/exchanges",
                headers=admin,
                params={**params, "cursor": listed.json()["next_cursor"]},
            )
            assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
            assert next_page.json()["items"][0]["id"] != item["id"]
        assert item["reason_code"] == "INVALID_ENVELOPE"
    receipt_only = await client.get(
        root + "/diagnostics",
        headers=admin,
        params={"peer": "WMS", "reference": receipt_only_id},
    )
    assert receipt_only.status_code == 200, receipt_only.text
    assert receipt_only.json()["items"] == []
    assert receipt_only.json()["runtime"][0]["receipt_id"] == 1202
    exchange_id = str(attempts["WMS"].id)
    detail = await client.get(f"{root}/exchanges/{exchange_id}", headers=admin)
    assert detail.status_code == 200, detail.text
    assert detail.json()["receipt_id"] == 1201
    assert any(
        ref["exchange_id"] == str(replay_id) and ref["related_exchange_id"] == exchange_id
        for ref in detail.json()["references"]
    )
    timeline = await client.get(f"{root}/exchanges/{exchange_id}/records", headers=admin)
    assert timeline.status_code == 200, timeline.text
    assert "fixture-body" not in timeline.text
    rows = {row["side"]: row for row in timeline.json()["items"]}
    assert rows["request"]["id"] == detail.json()["request_record_id"]
    assert rows["response"]["id"] == detail.json()["current_response_record_id"]
    paths = {side: f"{root}/records/{row['id']}/body" for side, row in rows.items()}
    auth_factory = app.state.audit_session_factory
    async with auth_factory.begin() as session:
        permission = await session.scalar(
            select(Permission.id).where(Permission.code == "integration:payload:read")
        )
        role = await session.scalar(select(Role.id).where(Role.name == "admin"))
        await session.execute(
            role_permission.delete().where(
                role_permission.c.role_id == role, role_permission.c.permission_id == permission
            )
        )
    assert (await client.get(paths["request"], headers=admin)).status_code == 403
    assert (
        await client.get(
            f"{root}/http-exchanges/WMS/{exchange_id}/request",
            headers=admin,
            params={"representation": "payload"},
        )
    ).status_code == 403
    async with auth_factory.begin() as session:
        assert (
            await session.scalar(
                select(AuditLog.id).where(AuditLog.action == "integration:payload:read")
            )
            is None
        )
        await session.execute(
            role_permission.insert().values(role_id=role, permission_id=permission)
        )
    request_body = await client.get(paths["request"], headers=admin)
    assert (
        request_body.content == wire and request_body.headers["x-integration-body-state"] == "FULL"
    )
    response_body = await client.get(paths["response"], headers=admin)
    assert response_body.status_code == 200 and response_body.content == b""
    structured = await client.get(
        f"{root}/http-exchanges/WMS/{exchange_id}/request",
        headers=admin,
        params={"representation": "payload"},
    )
    assert structured.json() == payload
    async with auth_factory() as session:
        logs = (
            await session.scalars(
                select(AuditLog).where(AuditLog.action == "integration:payload:read")
            )
        ).all()
        assert len(logs) == 3
        assert all("fixture-body" not in str(log.detail) and log.user_id for log in logs)
        assert all(
            log.detail["exchange_id"] == exchange_id and log.detail["peer"] == "WMS" for log in logs
        )
        by_path = {log.detail["route"]: log for log in logs}
        legacy_body = by_path[f"{root}/records/{{record_id}}/body"]
        assert legacy_body.detail["record_id"] == rows[legacy_body.detail["side"]]["id"]
        payload_read = by_path[f"{root}/http-exchanges/{{peer}}/{{exchange_id}}/{{side}}"]
        assert payload_read.detail["side"] == "request"
        assert payload_read.detail["representation"] == "payload"
    async with cutover() as session:
        assert await session.scalar(text("SELECT count(*) FROM integration_exchange_summary")) == 4
        assert await session.scalar(text("SELECT count(*) FROM integration_record_summary")) == 8
        for name in ("integration_exchange_summary", "integration_record_summary"):
            result = await session.execute(text(f"SELECT * FROM {name} LIMIT 0"))
            assert not set(result.keys()) & {
                "request_wire",
                "response_wire",
                "request_payload",
                "response_payload",
                "raw_body",
            }
    engine = cutover.kw["bind"]
    role = "history_meta_" + uuid7().hex
    async with engine.begin() as connection:
        await connection.execute(text(f"CREATE ROLE {role}"))
        await connection.execute(text(f"GRANT USAGE ON SCHEMA enterprise_backend TO {role}"))
        await connection.execute(
            text(
                "GRANT SELECT ON integration_exchange_summary, integration_record_summary "
                f"TO {role}"
            )
        )
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            assert (
                await connection.scalar(text("SELECT count(*) FROM integration_exchange_summary"))
                == 4
            )
            with pytest.raises(DBAPIError, match="permission denied"):
                async with connection.begin_nested():
                    await connection.execute(text("SELECT request_wire FROM wms_http_exchange"))
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP OWNED BY {role}"))
            await connection.execute(text(f"DROP ROLE {role}"))


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_runtime_expand")],
    indirect=True,
)
async def test_retirement_refuses_dependencies_then_drops_only_old_tables(
    cutover, tmp_path, monkeypatch
):
    proof = tmp_path / "proof.json"
    evidence = (
        dict.fromkeys(WRITERS, "DRAINED")
        | dict.fromkeys(CHECKS, "RETIRED")
        | {field: "isolated-fixture" for field in PROOFS}
    )
    proof.write_text(
        json.dumps(
            {key: value for key, value in evidence.items() if key != "integration_acceptance"}
        )
    )
    with pytest.raises(RetirementRefused, match="缺少"):
        read_evidence(proof)
    proof.write_text(json.dumps(evidence | {"manual": "UNKNOWN"}))
    with pytest.raises(RetirementRefused, match="排空"):
        read_evidence(proof)
    proof.write_text(json.dumps(evidence | {"backfill": "ACTIVE"}))
    with pytest.raises(RetirementRefused, match="依赖"):
        read_evidence(proof)
    proof.write_text(json.dumps(evidence))
    evidence = read_evidence(proof)
    engine = cutover.kw["bind"]
    exchange_id = uuid7()
    async with cutover.begin() as session:
        session.add(
            WmsHttpExchange(
                id=exchange_id,
                direction="OUTBOUND",
                interaction_kind="DECISION",
                traffic_source="PEER",
                started_at=datetime.now(UTC),
                http_method="POST",
                endpoint_path="/decisions",
                request_wire_state="UNAVAILABLE",
                response_wire_state="UNAVAILABLE",
                attempt_state="OPEN",
            )
        )
    async with engine.begin() as writer:
        await writer.execute(text("SELECT 1 FROM reliable_message"))
        with pytest.raises(RetirementRefused, match="数据库事务"):
            async with engine.begin() as connection:
                await retire(connection, evidence, apply=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE VIEW old_reader AS SELECT id FROM integration_exchange")
        )
        await connection.execute(
            text("CREATE TABLE old_owner (exchange_id uuid REFERENCES integration_exchange(id))")
        )
        await connection.execute(
            text("""
            CREATE FUNCTION old_function() RETURNS bigint LANGUAGE SQL
            BEGIN ATOMIC SELECT count(*) FROM integration_exchange; END
        """)
        )
    with pytest.raises(
        RetirementRefused, match="foreign_key old_owner.*routine old_function.*view old_reader"
    ):
        async with engine.begin() as connection:
            await retire(connection, evidence, apply=False)
    async with engine.begin() as connection:
        for name in TABLES:
            assert await connection.scalar(text("SELECT to_regclass(:name)"), {"name": name})
        await connection.execute(text("DROP VIEW old_reader"))
        await connection.execute(text("DROP TABLE old_owner"))
        await connection.execute(text("DROP FUNCTION old_function()"))
    async with engine.begin() as connection:
        await retire(connection, evidence, apply=False)
        assert await connection.scalar(text("SELECT to_regclass('integration_exchange')"))
    async with engine.begin() as connection:
        await retire(connection, evidence, apply=True)
    await require_reconciled(cutover)
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", False)
    with pytest.raises(RuntimeError, match="requires INTEGRATION_RUNTIME_AUTHORITY=true"):
        await require_reconciled(cutover)
    async with engine.connect() as connection:
        for name in TABLES:
            assert (
                await connection.scalar(text("SELECT to_regclass(:name)"), {"name": name}) is None
            )
        assert await connection.scalar(text("SELECT id FROM integration_exchange_summary")) == (
            exchange_id
        )
        assert await connection.scalar(
            text("SELECT exchange_id FROM integration_record_summary")
        ) == (exchange_id)
        assert (
            await connection.scalar(text("SELECT count(*) FROM integration_history_retirement"))
            == 1
        )
