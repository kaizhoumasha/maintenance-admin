import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text

from app.database import create_database_engine

HEAD = "11e090516bf1"
BASE = "a025a20261025"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_device_migration_preserves_history_and_empty_schema_roundtrips(backend, tmp_path):
    if backend == "postgres":
        url = os.environ.get("DEVICE_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("requires a separate disposable DEVICE_MIGRATION_DATABASE_URL")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}"

    async def migrate(direction, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url, "RELIABLE_TARGETS": "{}"},
            capture_output=True,
            text=True,
        )

    up = await migrate("upgrade", HEAD)
    assert up.returncode == 0, up.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as conn:
            tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert {"devices", "device_routes", "work_lines"} <= set(tables)
            assert "device_observations" not in tables
            await conn.execute(
                text(
                    "INSERT INTO device_routes (id,device_code,ecs_origin) VALUES "
                    "('route-test','D1','http://127.0.0.1')"
                )
            )
        refused = await migrate("downgrade", BASE)
        assert refused.returncode != 0
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM device_routes")) == 1
            assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            await conn.execute(text("DELETE FROM device_routes WHERE id='route-test'"))
    finally:
        await engine.dispose()
    down = await migrate("downgrade", BASE)
    assert down.returncode == 0, down.stderr
    again = await migrate("upgrade", HEAD)
    assert again.returncode == 0, again.stderr
