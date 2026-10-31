import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import MetaData, Table, inspect, null, select

from app.core.identifiers import uuid7
from app.database import create_database_engine


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_position_observation_upgrade_preserves_original_tasks(backend, tmp_path):
    if backend == "postgres":
        url = os.environ.get("TRANSPORT_OWNER_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("requires separate disposable TRANSPORT_OWNER_MIGRATION_DATABASE_URL")
        assert url != os.environ.get("TEST_DATABASE_URL")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'position-observation.db'}"

    async def migrate(direction, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url, "RELIABLE_TARGETS": "{}"},
            capture_output=True,
            text=True,
        )

    before = "f0b120261030"
    assert (reply := await migrate("upgrade", before)).returncode == 0, reply.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as connection:
            original_table = await connection.run_sync(
                lambda c: Table("transport_task", MetaData(), autoload_with=c)
            )
            await connection.execute(
                original_table.insert().values(
                    client_request_id=str(uuid7()),
                    transport_task_id="observation-migration",
                    operation_id=str(uuid7()),
                    kind="RACK_MOVE",
                    input_digest="a" * 64,
                    workline_id="line",
                    created_at=datetime.now(UTC),
                    owner_domain="original",
                    frozen={"kind": "RACK_MOVE", "rack_id": "R1"},
                )
            )
            original = (await connection.execute(select(original_table))).mappings().one()
        assert (reply := await migrate("upgrade", "head")).returncode == 0, reply.stderr
        async with engine.begin() as connection:
            columns = await connection.run_sync(lambda c: inspect(c).get_columns("transport_task"))
            assert any(c["name"] == "position_observation" and c["nullable"] for c in columns)
            current = await connection.run_sync(
                lambda c: Table("transport_task", MetaData(), autoload_with=c)
            )
            row = (await connection.execute(select(current))).mappings().one()
            assert row["position_observation"] is None
            assert {key: row[key] for key in original} == dict(original)
            await connection.execute(
                current.update().values(position_observation={"outcome_revision": 1})
            )
        refused = await migrate("downgrade", before)
        assert refused.returncode != 0
        async with engine.begin() as connection:
            assert await connection.scalar(select(current.c.position_observation)) == {
                "outcome_revision": 1
            }
            await connection.execute(current.update().values(position_observation=null()))
        assert (reply := await migrate("downgrade", before)).returncode == 0, reply.stderr
        assert (reply := await migrate("upgrade", "head")).returncode == 0, reply.stderr
        async with engine.begin() as connection:
            assert await connection.scalar(select(current.c.owner_domain)) == "original"
            assert await connection.scalar(select(current.c.position_observation)) is None
            await connection.execute(current.delete())
        assert (
            reply := await migrate("downgrade", "base" if backend == "postgres" else before)
        ).returncode == 0, reply.stderr
    finally:
        async with engine.begin() as connection:
            if await connection.run_sync(lambda c: inspect(c).has_table("transport_task")):
                await connection.execute(
                    original_table.delete().where(
                        original_table.c.transport_task_id == "observation-migration"
                    )
                )
        await engine.dispose()
