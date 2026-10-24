import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine as create_async_engine
from app.models import CallbackReceipt, CallbackSnapshot

BASE = "a023a20261024"
REVISION = "a024a20261024"


@pytest.fixture(params=["sqlite", "pg"])
async def callback_db(request, tmp_path):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'ecs.db'}"
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
    # 共享 PostgreSQL 库可能已在更新的 head 上:这里把库收敛到本迁移版本
    targeted = await migrate("downgrade", REVISION)
    assert targeted.returncode == 0, targeted.stdout + targeted.stderr
    engine = create_async_engine(url)
    try:
        yield engine, migrate
    finally:
        async with engine.begin() as connection:
            await connection.execute(CallbackSnapshot.__table__.delete())
            await connection.execute(CallbackReceipt.__table__.delete())
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr


def receipt(operation_id):
    return CallbackReceipt.__table__.insert().values(
        operation="ecs.event@v1",
        operation_id=operation_id,
        raw_digest="a" * 64,
        effective_digest=None,
        compare_mode="RAW",
        status_code=200,
        body={"code": "OK"},
        resp_timestamp=1792656000000,
        created_at=datetime.now(UTC),
    )


def snapshot(source):
    return CallbackSnapshot.__table__.insert().values(
        request_id="req-1",
        received_at=datetime.now(UTC),
        body_b64="e30=",
        truncated=False,
        http_status=200,
        outcome="ok",
        source=source,
    )


async def test_upgrade_accepts_64_char_receipt_identity_and_snapshot_source(callback_db):
    engine, _ = callback_db
    async with async_sessionmaker(engine).begin() as session:
        await session.execute(receipt("a" * 64))
        await session.execute(snapshot("ecs"))
    async with async_sessionmaker(engine)() as session:
        assert await session.scalar(select(CallbackReceipt.operation_id)) == "a" * 64
        assert await session.scalar(select(CallbackSnapshot.source)) == "ecs"


async def test_downgrade_is_refused_when_64_char_identity_or_source_exists(callback_db):
    engine, migrate = callback_db
    factory = async_sessionmaker(engine)
    async with factory.begin() as session:
        await session.execute(receipt("a" * 64))
        await session.execute(snapshot("ecs"))
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0
    async with factory.begin() as session:
        assert await session.scalar(select(CallbackReceipt.operation_id)) == "a" * 64
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        await session.execute(CallbackReceipt.__table__.delete())
    still_refused = await migrate("downgrade", BASE)
    assert still_refused.returncode != 0
    async with factory.begin() as session:
        assert await session.scalar(select(CallbackSnapshot.source)) == "ecs"


async def test_empty_schema_roundtrips(callback_db):
    _, migrate = callback_db
    down = await migrate("downgrade", BASE)
    assert down.returncode == 0, down.stdout + down.stderr
    up = await migrate("upgrade", REVISION)
    assert up.returncode == 0, up.stdout + up.stderr
