from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.integrations import history
from app.models import IntegrationExchange
from app.reliable.config import TargetConfig
from app.reliable.types import OutgoingMessage
from tests.helpers import run_alembic


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_expand")],
    indirect=True,
)
async def test_corrupt_frozen_request_blocks_authority_upgrade(state_migration):
    migration = state_migration
    factory = async_sessionmaker(migration.engine)
    target = TargetConfig("test", "http://example.test", "wms-decisions")
    # Use the previous revision's public schema. New runtime mappings are deliberately
    # not used to insert a message before the authority upgrade.
    raw = b'{"data":{"value":1}}'
    async with factory.begin() as session:
        now = datetime.now(UTC)
        operation_id = str(uuid7())
        exchange, request = await history.register(
            session,
            message=OutgoingMessage(
                target.name,
                "test",
                operation_id,
                raw,
                None,
                None,
                now,
                None,
            ),
            protocol=target.protocol,
        )
        await session.execute(
            text(
                "INSERT INTO reliable_message (target,operation,operation_id,status,attempts,"
                "next_attempt_at,created_at,updated_at,exchange_id,request_record_id) "
                "VALUES ('test','test',:operation_id,'PENDING',0,:now,:now,:now,:exchange,:request)"
            ),
            dict(operation_id=operation_id, now=now, exchange=exchange, request=request),
        )
        await session.execute(
            text("UPDATE integration_record SET body_sha256 = :sha WHERE id = :id"),
            {"sha": "0" * 64, "id": request},
        )
    result = await run_alembic(
        migration.engine.url.render_as_string(hide_password=False),
        "upgrade",
        "http_exchange_request_authority",
    )
    assert result.returncode != 0
    assert "frozen request integrity" in result.stdout + result.stderr
    async with factory.begin() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "http_exchange_expand"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='enterprise_backend' "
                    "AND table_name='reliable_message' AND column_name='frozen_request_wire'"
                )
            )
            == 0
        )
        await session.execute(
            text("UPDATE integration_record SET body_sha256 = :sha WHERE id = :id"),
            {"sha": sha256(raw).hexdigest(), "id": request},
        )
    await migration.migrate("upgrade", "http_exchange_request_authority")
    async with factory() as session:
        assert await session.scalar(text("SELECT frozen_request_wire FROM reliable_message")) == raw


@pytest.mark.parametrize(
    "state_migration",
    [("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "http_exchange_expand")],
    indirect=True,
)
async def test_missing_active_ecs_opening_blocks_authority_upgrade(state_migration):
    migration = state_migration
    factory = async_sessionmaker(migration.engine)
    now = datetime.now(UTC)
    code = str(uuid7())
    root_id = uuid7()
    lease = "legacy-ecs-lease"
    raw = b'{"device_code":"D1","task_type":"MOVE_FORWARD","params":{}}'
    async with factory.begin() as session:
        root = IntegrationExchange(
            id=root_id,
            role="ECS_COMMAND",
            peer_system="ECS",
            protocol="ecs-command",
            direction="OUTBOUND",
            interaction_kind="COMMAND",
            identity_kind="COMMAND_CODE",
            identity_value=code,
            command_code=code,
            owner_domain="test",
            call_key="call-key",
            call_key_digest=sha256(b"call-key").hexdigest(),
            input_raw=raw,
            created_at=now,
            dispatch_started_at=now,
            status="DISPATCHING",
            delivery_status="SENDING",
            request_sha256=sha256(raw).hexdigest(),
            device_code="D1",
            task_type="MOVE_FORWARD",
        )
        session.add(root)
        await session.flush()
        request_id = await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root_id,
                record_kind="REQUEST_REGISTERED",
                event_key="REQUEST",
                direction="OUTBOUND",
                occurred_at=now,
                raw_body=raw,
            ),
        )
        root.request_record_id = request_id
        await session.execute(
            text(
                "INSERT INTO reliable_message (target,operation,operation_id,status,attempts,"
                "next_attempt_at,created_at,updated_at,exchange_id,request_record_id,lease_id,"
                "lease_until) VALUES ('ecs','ecs.command',:code,'SENDING',0,:now,:now,:now,"
                ":root,:request,:lease,:now)"
            ),
            dict(code=code, now=now, root=root_id, request=request_id, lease=lease),
        )
    result = await run_alembic(
        migration.engine.url.render_as_string(hide_password=False),
        "upgrade",
        "http_exchange_request_authority",
    )
    assert result.returncode != 0
    assert "active ECS opening" in result.stdout + result.stderr
    async with factory.begin() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "http_exchange_expand"
        )
        assert await session.scalar(text("SELECT count(*) FROM reliable_delivery_attempt")) == 0
        open_id = await history.write_record(
            session,
            draft=history.RecordDraft(
                exchange_id=root_id,
                record_kind="SEND_ATTEMPT_OPENED",
                event_key=f"OPEN:{lease}",
                attempt_id=lease,
                parent_record_id=request_id,
                direction="OUTBOUND",
                occurred_at=now,
            ),
        )
    await migration.migrate("upgrade", "http_exchange_request_authority")
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, lease_id, started_at, attempt_state FROM reliable_delivery_attempt"
                )
            )
        ).one()
        assert row == (open_id, lease, now, "OPEN")
