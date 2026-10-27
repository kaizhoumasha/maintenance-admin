import asyncio
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from app.database import create_database_engine


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_existing_worklines_receive_empty_bindings_and_bound_data_blocks_downgrade(
    backend, tmp_path
):
    if backend == "postgres":
        url = os.environ.get("BINDING_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("requires a separate disposable BINDING_MIGRATION_DATABASE_URL")
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'bindings.db'}"

    async def migrate(direction, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url, "RELIABLE_TARGETS": "{}"},
            capture_output=True,
            text=True,
        )

    base = await migrate("upgrade", "11e090516bf1")
    assert base.returncode == 0, base.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO work_lines (line_code,line_name) VALUES ('L1','Line')")
            )
        up = await migrate("upgrade", "head")
        assert up.returncode == 0, up.stderr
        async with engine.begin() as conn:
            row = (
                await conn.execute(text("SELECT device_bindings,position_bindings FROM work_lines"))
            ).one()
            assert all((json.loads(v) if isinstance(v, str) else v) == {} for v in row)
            await conn.execute(
                text("UPDATE work_lines SET device_bindings='{" + '"SCAN1":"D1"' + "}'")
            )
        refused = await migrate("downgrade", "11e090516bf1")
        assert refused.returncode != 0
        async with engine.begin() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version")) == "c31f20261027"
            )
            await conn.execute(text("UPDATE work_lines SET device_bindings='{}'"))
        down = await migrate("downgrade", "11e090516bf1")
        assert down.returncode == 0, down.stderr
        again = await migrate("upgrade", "head")
        assert again.returncode == 0, again.stderr
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM work_lines WHERE line_code='L1'"))
        cleanup = await migrate("downgrade", "11e090516bf1")
        assert cleanup.returncode == 0, cleanup.stderr
    finally:
        await engine.dispose()
