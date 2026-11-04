import pytest
from sqlalchemy import text

from app.database import create_database_engine
from tests.helpers import run_alembic

BASE = "f0b220261031"
REVISION = "a39a1988a81a"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_enable_upgrade_preserves_lines_and_disabled_state(backend, tmp_path, request):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'enabled.db'}"
        if backend == "sqlite"
        else request.getfixturevalue("pg_url")
    )

    async def migrate(direction, revision):
        return await run_alembic(url, direction, revision)

    for direction in ("upgrade", "downgrade"):
        result = await migrate(direction, BASE)
        assert result.returncode == 0, result.stdout + result.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO work_lines (line_code,line_name,business_code) "
                    "VALUES ('ENABLE_OLD','Original','smt_manual')"
                )
            )
            original = (
                await connection.execute(
                    text(
                        "SELECT id,line_code,line_name,business_code,version FROM work_lines "
                        "WHERE line_code='ENABLE_OLD'"
                    )
                )
            ).one()
        result = await migrate("upgrade", REVISION)
        assert result.returncode == 0, result.stdout + result.stderr
        async with engine.begin() as connection:
            current = (
                await connection.execute(
                    text(
                        "SELECT id,line_code,line_name,business_code,version,is_enabled "
                        "FROM work_lines "
                        "WHERE line_code='ENABLE_OLD'"
                    )
                )
            ).one()
            assert tuple(current[:-1]) == tuple(original) and bool(current[-1]) is True
            await connection.execute(
                text("UPDATE work_lines SET is_enabled=false WHERE line_code='ENABLE_OLD'")
            )
        async with engine.connect() as connection:
            assert not await connection.scalar(
                text("SELECT is_enabled FROM work_lines WHERE line_code='ENABLE_OLD'")
            )
        refused = await migrate("downgrade", BASE)
        assert refused.returncode != 0
        assert "disabled worklines exist" in refused.stdout + refused.stderr
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM work_lines WHERE line_code='ENABLE_OLD'"))
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr
