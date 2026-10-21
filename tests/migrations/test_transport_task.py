import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import TransportTask

BASE = "a019c20261022"
REVISION = "a019d20261022"


@pytest.fixture(params=["sqlite", "pg"])
async def transport_db(request, tmp_path):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'transport.db'}"
        if request.param == "sqlite"
        else request.getfixturevalue("pg_url")
    )

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
        async with engine.begin() as connection:
            await connection.execute(TransportTask.__table__.delete())
        await engine.dispose()
        if request.param == "pg":
            restored = await migrate("upgrade", "head")
            assert restored.returncode == 0, restored.stdout + restored.stderr


async def test_identity_constraints_and_safe_downgrade(transport_db):
    engine, migrate = transport_db
    factory = async_sessionmaker(engine)
    values = dict(
        client_request_id=str(uuid.uuid4()),
        transport_task_id="transport-" + str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
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
            name: str(uuid.uuid4())
            for name in ("client_request_id", "transport_task_id", "operation_id")
            if name != column
        }
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await session.execute(insert(TransportTask).values(**changed))
    async with engine.connect() as connection:
        before = list((await connection.execute(select(TransportTask))).all())
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0 and "非空" in refused.stdout + refused.stderr
    async with engine.begin() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar() == REVISION
        assert list((await connection.execute(select(TransportTask))).all()) == before
        await connection.execute(TransportTask.__table__.delete())
    removed = await migrate("downgrade", BASE)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    async with engine.connect() as connection:
        assert not await connection.run_sync(lambda sync: inspect(sync).has_table("transport_task"))
    restored = await migrate("upgrade", REVISION)
    assert restored.returncode == 0, restored.stdout + restored.stderr
