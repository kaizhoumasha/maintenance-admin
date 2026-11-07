import pytest
from sqlalchemy import text

from app.database import create_database_engine
from tests.helpers import run_alembic


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_ecs_schema_roundtrip_and_guard_preserve_static_data(backend, tmp_path, request):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'ecs_migration.db'}"
        if backend == "sqlite"
        else request.getfixturevalue("pg_url")
    )

    async def migrate(command, revision):
        return await run_alembic(url, command, revision)

    result = await migrate("upgrade", "a025a20261025")
    assert result.returncode == 0, result.stderr
    result = await migrate("downgrade", "a025a20261025")
    assert result.returncode == 0, result.stdout + result.stderr
    engine = create_database_engine(url)
    try:
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO reliable_message "
                    "(target,operation,operation_id,request_raw,status,attempts,"
                    "next_attempt_at,created_at,updated_at) "
                    "VALUES "
                    "('ecs-migration','opaque','old',:raw,'PENDING',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {"raw": b"frozen"},
            )
        down = await migrate("downgrade", "a024a20261024")
        assert down.returncode == 0, down.stderr
        up = await migrate("upgrade", "a025a20261025")
        assert up.returncode == 0, up.stderr
        async with engine.begin() as c:
            assert (
                await c.execute(
                    text("SELECT request_raw FROM reliable_message WHERE target='ecs-migration'")
                )
            ).scalar() == b"frozen"
            await c.execute(
                text(
                    "UPDATE reliable_message SET "
                    "status='HELD',pause_reason='DELIVERY_UNKNOWN',next_attempt_at=NULL WHERE "
                    "target='ecs-migration'"
                )
            )
        refused = await migrate("downgrade", "a024a20261024")
        assert refused.returncode != 0 and "refusing" in (refused.stdout + refused.stderr)
        async with engine.begin() as c:
            assert (
                await c.execute(
                    text("SELECT status FROM reliable_message WHERE target='ecs-migration'")
                )
            ).scalar() == "HELD"
    finally:
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM reliable_message WHERE target='ecs-migration'"))
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr
