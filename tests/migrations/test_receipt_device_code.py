import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text

from app.database import create_database_engine

BASE = "d7cf20261029"
REVISION = "ea8b20261029"


@pytest.fixture(params=["sqlite", "postgres"])
async def receipt_migration(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'receipt-device.db'}"
    else:
        url = os.environ.get("RECEIPT_DEVICE_MIGRATION_DATABASE_URL")
        if not url:
            pytest.skip("requires a separate disposable RECEIPT_DEVICE_MIGRATION_DATABASE_URL")

    async def migrate(command, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", command, revision],
            env={**os.environ, "DATABASE_URL": url, "RELIABLE_TARGETS": "{}"},
            capture_output=True,
            text=True,
        )

    for command in ("upgrade", "downgrade"):
        result = await migrate(command, BASE)
        assert result.returncode == 0, result.stdout + result.stderr
    engine = create_database_engine(url)
    try:
        yield engine, migrate
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM callback_receipt"))
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr


async def test_upgrade_preserves_legacy_receipt_and_downgrade_protects_device(receipt_migration):
    engine, migrate = receipt_migration
    legacy_sql = text(
        "INSERT INTO callback_receipt "
        "(operation,operation_id,raw_digest,effective_digest,compare_mode,status_code,body,"
        "resp_timestamp,created_at) "
        "VALUES ('ecs.event','legacy-receipt',:digest,NULL,'RAW',200,:body,1,CURRENT_TIMESTAMP)"
    )
    original_query = text(
        "SELECT id,operation,operation_id,raw_digest,status_code,body,resp_timestamp,created_at "
        "FROM callback_receipt ORDER BY id"
    )
    async with engine.begin() as connection:
        await connection.execute(legacy_sql, {"digest": "a" * 64, "body": '{"code":200}'})
        original = (await connection.execute(original_query)).all()
    result = await migrate("upgrade", REVISION)
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.begin() as connection:
        assert (await connection.execute(original_query)).all() == original
        assert await connection.scalar(text("SELECT device_code FROM callback_receipt")) is None
        indexes = await connection.run_sync(
            lambda sync: inspect(sync).get_indexes("callback_receipt")
        )
        assert any(index["column_names"] == ["device_code"] for index in indexes)
        await connection.execute(text("UPDATE callback_receipt SET device_code='RS-INPUT-ARM-01'"))
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        assert await connection.scalar(text("SELECT device_code FROM callback_receipt")) == (
            "RS-INPUT-ARM-01"
        )
        await connection.execute(text("UPDATE callback_receipt SET device_code=NULL"))
    down = await migrate("downgrade", BASE)
    assert down.returncode == 0, down.stdout + down.stderr
    async with engine.begin() as connection:
        assert (await connection.execute(original_query)).all() == original
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_columns("callback_receipt")
        )
        assert "device_code" not in {column["name"] for column in columns}
    again = await migrate("upgrade", REVISION)
    assert again.returncode == 0, again.stdout + again.stderr
    async with engine.begin() as connection:
        assert (await connection.execute(original_query)).all() == original
        assert await connection.scalar(text("SELECT device_code FROM callback_receipt")) is None
