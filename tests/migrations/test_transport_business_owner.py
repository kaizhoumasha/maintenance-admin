import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData, Table, inspect, select, text

from app.core.identifiers import uuid7
from app.database import create_database_engine


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_transport_owner_upgrade_preserves_rows_and_fences_downgrade(backend, tmp_path):
    if backend == "postgres":
        url = os.environ.get("TRANSPORT_OWNER_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("requires separate disposable TRANSPORT_OWNER_MIGRATION_DATABASE_URL")
        assert url != os.environ.get("TEST_DATABASE_URL"), "migration tests need their own database"
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'transport-owner.db'}"
    before_owner = "ea8b20261029"

    async def migrate(direction, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url, "RELIABLE_TARGETS": "{}"},
            capture_output=True,
            text=True,
        )

    assert (result := await migrate("upgrade", before_owner)).returncode == 0, result.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as conn:
            task_table = await conn.run_sync(
                lambda c: Table("transport_task", MetaData(), autoload_with=c)
            )
            message_table = await conn.run_sync(
                lambda c: Table("reliable_message", MetaData(), autoload_with=c)
            )
            identity, operation = str(uuid7()), str(uuid7())
            now = datetime.now(UTC)
            await conn.execute(
                task_table.insert().values(
                    client_request_id=identity,
                    transport_task_id="legacy-owner-task",
                    operation_id=operation,
                    kind="RACK_MOVE",
                    input_digest="a" * 64,
                    workline_id="line",
                    created_at=now,
                    cycle_id=str(uuid7()),
                    frozen={"kind": "RACK_MOVE", "rack_id": "R1"},
                )
            )
            await conn.execute(
                message_table.insert().values(
                    target="migration-test",
                    operation="transport.task.submit@v1",
                    operation_id=operation,
                    request_raw=b"original frozen wire bytes",
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=now,
                    business_key="legacy-owner-task",
                    created_at=now,
                    updated_at=now,
                )
            )
            original = (await conn.execute(select(task_table))).mappings().one()
            frozen_raw = await conn.scalar(select(message_table.c.request_raw))
        assert (result := await migrate("upgrade", "head")).returncode == 0, result.stderr
        async with engine.begin() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("transport_task"))
            assert any(c["name"] == "owner_domain" and c["nullable"] for c in columns)
            current = await conn.run_sync(
                lambda c: Table("transport_task", MetaData(), autoload_with=c)
            )
            after = (await conn.execute(select(current))).mappings().one()
            assert after["owner_domain"] is None
            assert {key: after[key] for key in original} == dict(original)
            assert await conn.scalar(select(message_table.c.request_raw)) == frozen_raw
            values = dict(original)
            values.pop("id")
            values.update(
                client_request_id=str(uuid7()),
                transport_task_id="owned-task",
                operation_id=str(uuid7()),
                owner_domain="first",
            )
            await conn.execute(current.insert().values(**values))
            head = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        refused = await migrate("downgrade", before_owner)
        assert refused.returncode != 0
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == head
            assert (
                await conn.scalar(
                    select(current.c.owner_domain).where(
                        current.c.transport_task_id == "owned-task"
                    )
                )
                == "first"
            )
            await conn.execute(current.delete().where(current.c.transport_task_id == "owned-task"))
        assert (result := await migrate("downgrade", before_owner)).returncode == 0, result.stderr
        assert (result := await migrate("upgrade", "head")).returncode == 0, result.stderr
        async with engine.begin() as conn:
            assert await conn.scalar(select(current.c.owner_domain)) is None
            assert await conn.scalar(select(message_table.c.request_raw)) == frozen_raw
            await conn.execute(current.delete())
            await conn.execute(message_table.delete())
        if backend == "postgres":
            assert (result := await migrate("downgrade", "base")).returncode == 0, result.stderr
            assert (result := await migrate("upgrade", "head")).returncode == 0, result.stderr
            assert (result := await migrate("downgrade", "base")).returncode == 0, result.stderr
        else:
            assert (result := await migrate("downgrade", before_owner)).returncode == 0, (
                result.stderr
            )
    finally:
        await engine.dispose()
