import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.database import create_database_engine

BASE = "c31f20261027"


@pytest.mark.parametrize("later_migrations", [0, 1, 2])
def test_workline_business_has_single_migration_head(tmp_path, later_migrations):
    config = Config("alembic.ini")
    if later_migrations:
        migration_path = tmp_path / "migrations"
        shutil.copytree("migrations", migration_path)
        config.set_main_option("script_location", str(migration_path))
        for index in range(later_migrations):
            current_head = ScriptDirectory.from_config(config).get_current_head()
            (migration_path / "versions" / f"next_revision_{index}.py").write_text(
                f"revision = 'test_future_revision_{index}'\ndown_revision = {current_head!r}\n"
            )
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert script.get_revision("a6d464b20ab7").down_revision == BASE
    assert "a6d464b20ab7" in {
        revision.revision for revision in script.iterate_revisions(heads[0], "base")
    }


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_old_line_stays_null_and_selected_business_blocks_downgrade(
    backend, tmp_path, request
):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'business.db'}"
        if backend == "sqlite"
        else request.getfixturevalue("pg_url")
    )

    async def migrate(direction, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )

    up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stdout + up.stderr
    down = await migrate("downgrade", BASE)
    assert down.returncode == 0, down.stdout + down.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO work_lines (line_code,line_name) VALUES ('BUSINESS_OLD','Old')")
            )
        up = await migrate("upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT business_code FROM work_lines WHERE line_code='BUSINESS_OLD'")
                )
                is None
            )
            await connection.execute(
                text(
                    "UPDATE work_lines SET business_code='test_sorting' "
                    "WHERE line_code='BUSINESS_OLD'"
                )
            )
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        refused = await migrate("downgrade", BASE)
        assert refused.returncode != 0
        assert "business selections exist" in refused.stdout + refused.stderr
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == head
            assert (
                await connection.scalar(
                    text("SELECT business_code FROM work_lines WHERE line_code='BUSINESS_OLD'")
                )
                == "test_sorting"
            )
            await connection.execute(text("DELETE FROM work_lines WHERE line_code='BUSINESS_OLD'"))
        down = await migrate("downgrade", BASE)
        assert down.returncode == 0, down.stdout + down.stderr
        up = await migrate("upgrade", "head")
        assert up.returncode == 0, up.stdout + up.stderr
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM work_lines WHERE line_code='BUSINESS_OLD'"))
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr
