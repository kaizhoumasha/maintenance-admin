import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

BASE = "a020a20261022"
REVISION = "a021a20261023"

# 迁移前的 transport_task / reliable_message:用不含新列的轻量表对象写入
OLD_TASK = sa.table(
    "transport_task",
    sa.column("client_request_id"),
    sa.column("transport_task_id"),
    sa.column("operation_id"),
    sa.column("kind"),
    sa.column("input_digest"),
    sa.column("workline_id"),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
NEW_TASK = sa.table(
    "transport_task",
    sa.column("client_request_id"),
    sa.column("transport_task_id"),
    sa.column("operation_id"),
    sa.column("kind"),
    sa.column("input_digest"),
    sa.column("workline_id"),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("outcome_status"),
)
MESSAGE = sa.table(
    "reliable_message",
    sa.column("target"),
    sa.column("operation"),
    sa.column("operation_id"),
    sa.column("request_raw"),
    sa.column("status"),
    sa.column("attempts"),
    sa.column("next_attempt_at", sa.DateTime(timezone=True)),
    sa.column("business_key"),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


@pytest.fixture(params=["sqlite", "pg"])
async def state_db(request, tmp_path):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"
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

    for step in ("upgrade", "downgrade"):  # 共享库可能已在 head:先收敛到迁移前版本
        done = await migrate(step, BASE)
        assert done.returncode == 0, done.stdout + done.stderr
    engine = create_async_engine(url)
    try:
        yield engine, migrate
    finally:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DELETE FROM reliable_message"))
            await connection.execute(sa.text("DELETE FROM transport_task"))
            await connection.execute(sa.text("DELETE FROM callback_receipt"))
        await engine.dispose()
        restored = await migrate("upgrade", "head")
        assert restored.returncode == 0, restored.stdout + restored.stderr


def old_task(task_id):
    return dict(
        client_request_id="c-" + task_id,
        transport_task_id=task_id,
        operation_id="o-" + task_id,
        kind="RACK_MOVE",
        input_digest="a" * 64,
        workline_id="line",
        created_at=datetime.now(UTC),
    )


async def test_upgrade_backfills_frozen_from_the_submitted_request(state_db):
    engine, migrate = state_db
    request = {
        "operation": "x",
        "data": {"transport_task_id": "t1", "kind": "RACK_MOVE", "rack_id": "R"},
    }
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        for name in ("t1", "t2"):
            await connection.execute(OLD_TASK.insert().values(**old_task(name)))
        await connection.execute(
            MESSAGE.insert().values(
                target="wms", operation="op", operation_id="m1", status="DONE", attempts=1,
                request_raw=json.dumps(request).encode(), business_key="t1",
                next_attempt_at=now, created_at=now, updated_at=now,
            )
        )  # fmt: skip
    upgraded = await migrate("upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    async with engine.connect() as connection:
        rows = dict(
            (
                await connection.execute(
                    sa.text("SELECT transport_task_id, frozen FROM transport_task")
                )
            ).all()
        )
    frozen = rows["t1"]
    assert (json.loads(frozen) if isinstance(frozen, str) else frozen) == {
        "kind": "RACK_MOVE",
        "rack_id": "R",
    }
    assert rows["t2"] is None  # 冻结消息已不存在的任务保持为空


async def test_downgrade_is_refused_once_callback_state_exists(state_db):
    engine, migrate = state_db
    upgraded = await migrate("upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    async with engine.begin() as connection:
        await connection.execute(
            NEW_TASK.insert().values(**old_task("t"), outcome_status="SUCCEEDED")
        )
    refused = await migrate("downgrade", BASE)
    assert refused.returncode != 0 and "禁止破坏性降级" in refused.stdout + refused.stderr
    async with engine.begin() as connection:
        await connection.execute(sa.text("DELETE FROM transport_task"))
    removed = await migrate("downgrade", BASE)
    assert removed.returncode == 0, removed.stdout + removed.stderr
