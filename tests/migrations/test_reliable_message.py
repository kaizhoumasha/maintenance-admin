from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, insert, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.database import create_database_engine
from app.integrations import history as history_records
from app.models import ReliableMessage
from tests.helpers import run_alembic
from tests.migration_helpers import isolated_schema_database


@pytest.mark.parametrize("occupied", [False, True])
async def test_message_downgrade_requires_empty_table(pg_url, occupied):
    from sqlalchemy import MetaData, Table, select

    async with isolated_schema_database(pg_url) as (url, _):
        result = await run_alembic(url, "upgrade", "a019b20261021")
        assert result.returncode == 0, result.stdout + result.stderr
        engine = create_database_engine(url)
        pg_sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.connect() as c:
                original = await c.run_sync(
                    lambda sync: Table("reliable_message", MetaData(), autoload_with=sync)
                )
            if occupied:
                async with pg_sessions.begin() as session:
                    await session.execute(
                        original.insert().values(
                            target="original",
                            operation="opaque",
                            operation_id="original",
                            request_raw=b"opaque",
                            status="PENDING",
                            attempts=0,
                            next_attempt_at=datetime.now(UTC),
                            created_at=datetime.now(UTC),
                            updated_at=datetime.now(UTC),
                        )
                    )
            result = await run_alembic(url, "downgrade", "c9f2b7a41d08")
            assert (result.returncode != 0) == occupied, result.stdout + result.stderr
            if occupied:
                assert "非空" in result.stdout + result.stderr
                async with pg_sessions.begin() as session:
                    assert await session.scalar(select(original.c.request_raw)) == b"opaque"
                    assert (
                        await session.scalar(text("SELECT version_num FROM alembic_version"))
                        == "a019b20261021"
                    )
                    await session.execute(original.delete())
            else:
                async with pg_sessions() as session:
                    assert (
                        await session.scalar(text("SELECT to_regclass('reliable_message')")) is None
                    )
        finally:
            await engine.dispose()


async def test_expiry_index_matches_model_and_bounded_range_scan(pg_sessions, record_property):
    engine = pg_sessions.kw["bind"]
    async with engine.connect() as connection:
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("reliable_message")
        )
        checks = await connection.run_sync(
            lambda sync: inspect(sync).get_check_constraints("reliable_message")
        )
    assert {check["name"] for check in checks} == {
        constraint.name
        for constraint in ReliableMessage.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert any(
        index["column_names"] == ["status", "expires_at"] and not index["unique"]
        for index in indexes
    )
    assert any(
        [column.name for column in index.columns] == ["status", "expires_at"]
        for index in ReliableMessage.__table__.indexes
    )
    target = "index-" + uuid7().hex
    now = datetime.now(UTC)
    try:
        async with pg_sessions() as session:
            from hashlib import sha256

            from app.models import IntegrationExchange, IntegrationRecord

            roots, records, messages = [], [], []
            for number in range(2000):
                root_id, record_id = uuid7(), uuid7()
                roots.append(
                    dict(
                        id=root_id,
                        role="OUTGOING",
                        protocol="opaque",
                        direction="OUTBOUND",
                        interaction_kind="REQUEST",
                        identity_kind="OPERATION_ID",
                        identity_value=str(number),
                        operation="opaque",
                        operation_id=str(number),
                        target_key=target,
                        created_at=now,
                    )
                )
                records.append(
                    dict(
                        id=record_id,
                        exchange_id=root_id,
                        record_kind="REQUEST_REGISTERED",
                        event_key="original",
                        direction="OUTBOUND",
                        raw_body=b"x",
                        body_state="FULL",
                        body_sha256=sha256(b"x").hexdigest(),
                        stored_bytes=1,
                    )
                )
                messages.append(
                    dict(
                        target=target,
                        operation="opaque",
                        operation_id=str(number),
                        exchange_id=root_id,
                        request_record_id=record_id,
                        status="PENDING",
                        attempts=0,
                        next_attempt_at=now + timedelta(days=1),
                        created_at=now,
                        updated_at=now,
                        expires_at=now + timedelta(days=1) if number > 2 else now,
                    )
                )
            await session.execute(IntegrationExchange.__table__.insert(), roots)
            await session.execute(IntegrationRecord.__table__.insert(), records)
            from sqlalchemy import update

            await session.execute(
                update(IntegrationExchange)
                .where(IntegrationExchange.target_key == target)
                .values(
                    request_record_id=select(IntegrationRecord.id)
                    .where(IntegrationRecord.exchange_id == IntegrationExchange.id)
                    .scalar_subquery()
                )
            )
            await session.execute(insert(ReliableMessage), messages)
            await session.commit()
        async with pg_sessions() as session:
            plan = list(
                await session.scalars(
                    text(
                        "EXPLAIN SELECT id FROM reliable_message "
                        "WHERE status='PENDING' AND expires_at <= :now "
                        "ORDER BY expires_at,id LIMIT 2 FOR UPDATE SKIP LOCKED"
                    ),
                    {"now": now},
                )
            )
            record_property("pending_expiry_explain", "\n".join(plan))
            assert plan
        from app.reliable.store import MessageStore

        store = MessageStore(pg_sessions, clock=lambda: now, records=history_records)
        assert await store.expire_pending(2) == 2
        assert await store.expire_pending(2) == 1
        assert await store.expire_pending(2) == 0
    finally:
        async with pg_sessions() as session:
            from app.models import IntegrationExchange
            from tests.helpers import clear_integration_history

            roots = list(
                await session.scalars(
                    select(IntegrationExchange.id).where(IntegrationExchange.target_key == target)
                )
            )
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == target))
            await clear_integration_history(session, roots)
            await session.commit()
