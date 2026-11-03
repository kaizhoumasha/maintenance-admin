import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.database import create_database_engine as create_async_engine
from app.models import TransportTask

BASE = "a019c20261022"
REVISION = "a019d20261022"


@pytest.fixture(params=["sqlite", "pg"])
def transport_migration_url(request, tmp_path):
    return (
        f"sqlite+aiosqlite:///{tmp_path / 'transport.db'}"
        if request.param == "sqlite"
        else request.getfixturevalue("schema_database")[0]
    )


@pytest.fixture
async def transport_db(transport_migration_url):
    url = transport_migration_url

    async def migrate(command, revision):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", command, revision],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )

    upgraded = await migrate("upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    engine = create_async_engine(url)
    try:
        yield engine, migrate
    finally:
        await engine.dispose()


async def test_identity_constraints_and_safe_downgrade(transport_db):
    engine, migrate = transport_db
    factory = async_sessionmaker(engine)
    values = dict(
        client_request_id=str(uuid7()),
        transport_task_id="transport-" + str(uuid7()),
        operation_id=str(uuid7()),
        kind="RACK_MOVE",
        input_digest="a" * 64,
        workline_id="line",
        station_id="station",
        created_at=datetime.now(UTC),
    )
    async with factory.begin() as session:
        await session.execute(insert(TransportTask).values(**values))
    for column in ("client_request_id", "transport_task_id", "operation_id"):
        changed = values | {
            name: str(uuid7())
            for name in ("client_request_id", "transport_task_id", "operation_id")
            if name != column
        }
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(insert(TransportTask).values(**changed))
    async with engine.connect() as connection:
        before = list(
            (
                await connection.execute(select(TransportTask.id, TransportTask.transport_task_id))
            ).all()
        )
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0 and "非空" in refused.stdout + refused.stderr
    async with engine.begin() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar() == REVISION
        rows = await connection.execute(select(TransportTask.id, TransportTask.transport_task_id))
        assert list(rows.all()) == before
        await connection.execute(TransportTask.__table__.delete())
    removed = await migrate("downgrade", BASE)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    async with engine.connect() as connection:
        assert not await connection.run_sync(lambda sync: inspect(sync).has_table("transport_task"))
    restored = await migrate("upgrade", REVISION)
    assert restored.returncode == 0, restored.stdout + restored.stderr


async def test_original_acceptance_survives_refused_downgrade(transport_db):
    engine, migrate = transport_db
    previous = await migrate("upgrade", "fiveracklimit")
    assert previous.returncode == 0, previous.stdout + previous.stderr
    async with engine.begin() as conn:
        await conn.execute(
            insert(TransportTask).values(
                transport_task_id="transport-" + str(uuid7()),
                client_request_id=str(uuid7()),
                operation_id=str(uuid7()),
                kind="RACK_MOVE",
                input_digest="a" * 64,
                workline_id="test",
                created_at=datetime.now(UTC),
            )
        )
    upgrade = await migrate("upgrade", "racksubmitack")
    assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    async with engine.begin() as conn:
        assert (
            await conn.scalar(
                select(TransportTask.input_digest).where(
                    TransportTask.submission_acceptance.is_(None)
                )
            )
            == "a" * 64
        )
        await conn.execute(
            TransportTask.__table__.update().values(submission_acceptance={"record": 1})
        )
    refused = await migrate("downgrade", "fiveracklimit")
    assert refused.returncode != 0 and "submission_acceptance" in refused.stdout + refused.stderr
    async with engine.begin() as conn:
        assert await conn.scalar(select(TransportTask.submission_acceptance)) == {"record": 1}
        await conn.execute(TransportTask.__table__.delete())
    assert (await migrate("downgrade", "fiveracklimit")).returncode == 0
