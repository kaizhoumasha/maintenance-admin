from contextlib import AsyncExitStack
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine as create_async_engine
from tests.helpers import run_alembic
from tests.migration_helpers import isolated_schema_database, legacy_callback_tables

receipts, _ = legacy_callback_tables()

BASE = "a019d20261022"
REVISION = "a020a20261022"


@pytest.fixture(params=["sqlite", "pg"])
async def callback_db(request, tmp_path):
    async with AsyncExitStack() as databases:
        if request.param == "sqlite":
            url = f"sqlite+aiosqlite:///{tmp_path / 'callback.db'}"
        else:
            url, _ = await databases.enter_async_context(
                isolated_schema_database(request.getfixturevalue("pg_url"))
            )

        async def migrate(command, revision):
            return await run_alembic(url, command, revision)

        upgraded = await migrate("upgrade", REVISION)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        engine = create_async_engine(url)
        try:
            yield engine, migrate
        finally:
            await engine.dispose()


async def test_receipt_identity_survives_refused_downgrade_and_empty_schema_roundtrips(callback_db):
    engine, migrate = callback_db
    factory = async_sessionmaker(engine)
    async with factory.begin() as session:
        await session.execute(
            receipts.insert().values(
                operation="unknown@v1",
                operation_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
                raw_digest="a" * 64,
                effective_digest=None,
                compare_mode="RAW",
                status_code=422,
                body={"code": "REJECTED"},
                resp_timestamp=1792656000000,
                created_at=datetime.now(UTC),
            )
        )
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0 and "非空" in refused.stdout + refused.stderr
    async with factory.begin() as session:
        assert await session.scalar(select(receipts.c.status_code)) == 422
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        await session.execute(receipts.delete())
    removed = await migrate("downgrade", BASE)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    async with engine.connect() as connection:
        assert not await connection.run_sync(
            lambda sync: inspect(sync).has_table("callback_receipt")
        )
        assert not await connection.run_sync(
            lambda sync: inspect(sync).has_table("callback_snapshot")
        )
    upgraded = await migrate("upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
