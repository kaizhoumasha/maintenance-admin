import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, insert, inspect, text

from app.core.identifiers import uuid7
from app.models import ReliableMessage


@pytest.mark.parametrize("occupied", [False, True])
async def test_message_downgrade_requires_empty_table(pg_url, pg_sessions, occupied):
    target = "migration-" + uuid7().hex
    now = datetime.now(UTC)
    if occupied:
        async with pg_sessions() as session:
            await session.execute(
                insert(ReliableMessage).values(
                    target=target,
                    operation="opaque",
                    operation_id="id",
                    request_raw=b"opaque",
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

    async def migrate(command, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", command, revision],
            env={**os.environ, "DATABASE_URL": pg_url},
            capture_output=True,
            text=True,
        )

    try:
        async with pg_sessions() as session:
            original_version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        result = await migrate("downgrade", "c9f2b7a41d08")
        assert (result.returncode != 0) == occupied, result.stdout + result.stderr
        if occupied:
            assert "非空" in result.stdout + result.stderr
            async with pg_sessions() as session:
                assert (
                    await session.scalar(text("SELECT version_num FROM alembic_version"))
                    == original_version
                )
        else:
            async with pg_sessions() as session:
                assert await session.scalar(text("SELECT to_regclass('reliable_message')")) is None
    finally:
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr
        async with pg_sessions() as session:
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == target))
            await session.commit()


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
            await session.execute(
                insert(ReliableMessage),
                [
                    dict(
                        target=target,
                        operation="opaque",
                        operation_id=str(number),
                        request_raw=b"x",
                        status="PENDING",
                        attempts=0,
                        next_attempt_at=now + timedelta(days=1),
                        created_at=now,
                        updated_at=now,
                        expires_at=now + timedelta(days=1) if number > 2 else now,
                    )
                    for number in range(2000)
                ],
            )
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

        store = MessageStore(pg_sessions, clock=lambda: now)
        assert await store.expire_pending(2) == 2
        assert await store.expire_pending(2) == 1
        assert await store.expire_pending(2) == 0
    finally:
        async with pg_sessions() as session:
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == target))
            await session.commit()
