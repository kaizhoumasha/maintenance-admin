"""STALLED 状态、未知事实标记与未送达计数的迁移:存量回填、约束、安全降级(说明书 §5)。"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

BASE, HEAD = "a019b20261021", "a019c20261022"
ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 10, 22, tzinfo=UTC)
OLD_INSERT = text(
    "INSERT INTO reliable_message (target, operation, operation_id, request_raw, status, "
    "attempts, next_attempt_at, lease_id, lease_until, created_at, updated_at) VALUES "
    "(:target, 'op', :oid, :raw, :status, 2, :now, :lease, :lease_until, :now, :now)"
)
NEW_INSERT = text(
    "INSERT INTO reliable_message (target, operation, operation_id, request_raw, status, "
    "attempts, next_attempt_at, delivery_uncertain, not_received_count, created_at, "
    "updated_at) VALUES (:target, 'op', :oid, :raw, :status, 2, :next, :uncertain, :count, "
    ":now, :now)"
)


def test_revision_discovery_has_one_head_without_running_migrations():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len([line for line in result.stdout.splitlines() if line.endswith(" (head)")]) == 1


async def migrate(url, command, revision):
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


async def query(url, sql, **params):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql) if isinstance(sql, str) else sql, params)
            return list(result.all()) if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest.fixture(params=["sqlite", "pg"])
async def db(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    else:
        url = request.getfixturevalue("pg_url")
        stepped = await migrate(url, "downgrade", BASE)
        assert stepped.returncode == 0, stepped.stdout + stepped.stderr
    prepared = await migrate(url, "upgrade", BASE)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    target = "stalled-" + uuid.uuid4().hex
    try:
        yield url, target
    finally:
        if request.param == "pg":
            restored = await migrate(url, "upgrade", "head")
            assert restored.returncode == 0, restored.stdout + restored.stderr
            await query(url, "DELETE FROM reliable_message WHERE target = :target", target=target)


def old_row(target, oid, status):
    leased = status == "SENDING"
    return dict(
        target=target,
        oid=oid,
        raw=b"\x00frozen" + oid.encode(),
        status=status,
        now=NOW,
        lease="lease-" + oid if leased else None,
        lease_until=NOW if leased else None,
    )


async def test_upgrade_backfills_unknown_for_active_messages_and_keeps_facts(db):
    url, target = db
    statuses = ("PENDING", "SENDING", "DONE", "FAILED", "EXPIRED")
    for status in statuses:
        await query(url, OLD_INSERT, **old_row(target, status.lower(), status))
    before = await query(
        url,
        "SELECT operation_id, request_raw, status, attempts, lease_id FROM reliable_message "
        "WHERE target = :target ORDER BY operation_id",
        target=target,
    )
    upgraded = await migrate(url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    after = await query(
        url,
        "SELECT operation_id, request_raw, status, attempts, lease_id, delivery_uncertain, "
        "not_received_count FROM reliable_message WHERE target = :target ORDER BY operation_id",
        target=target,
    )
    assert [tuple(r)[:5] for r in after] == [tuple(r) for r in before]
    assert {r.status: (bool(r.delivery_uncertain), r.not_received_count) for r in after} == {
        "PENDING": (True, 0),
        "SENDING": (True, 0),
        "DONE": (False, 0),
        "FAILED": (False, 0),
        "EXPIRED": (False, 0),
    }
    fresh = dict(old_row(target, "fresh", "PENDING"), next=NOW, uncertain=False, count=0)
    await query(url, NEW_INSERT, **{k: v for k, v in fresh.items() if k in NEW_INSERT._bindparams})
    (default,) = await query(
        url,
        "SELECT delivery_uncertain FROM reliable_message WHERE operation_id = 'fresh' "
        "AND target = :target",
        target=target,
    )
    assert bool(default.delivery_uncertain) is False


async def test_upgraded_schema_allows_stalled_without_schedule_only(db):
    url, target = db
    assert (await migrate(url, "upgrade", "head")).returncode == 0
    base = dict(old_row(target, "x", "PENDING"), uncertain=False, count=0)
    stalled = dict(base, oid="stalled", status="STALLED", next=None)
    columns = {k: v for k, v in stalled.items() if k in NEW_INSERT._bindparams}
    await query(url, NEW_INSERT, **columns)
    for oid, status, schedule in (("p", "PENDING", None), ("bad", "NOPE", NOW)):
        broken = dict(base, oid=oid, status=status, next=schedule)
        with pytest.raises(DBAPIError):
            await query(
                url, NEW_INSERT, **{k: v for k, v in broken.items() if k in NEW_INSERT._bindparams}
            )


@pytest.mark.parametrize(
    "blocker,status,uncertain,count,schedule",
    [
        ("stalled", "STALLED", False, 3, None),
        ("uncertain", "PENDING", True, 0, NOW),
        ("counted", "SENDING", False, 1, NOW),
    ],
)
async def test_downgrade_refuses_to_drop_safety_facts_then_succeeds_without_them(
    db, blocker, status, uncertain, count, schedule
):
    url, target = db
    assert (await migrate(url, "upgrade", "head")).returncode == 0
    safe = [
        ("clean", "PENDING", False, 0, NOW),
        ("done", "DONE", True, 2, NOW),
    ]
    rows = [(blocker, status, uncertain, count, schedule), *safe]
    for oid, state, flag, counted, next_at in rows:
        values = dict(old_row(target, oid, state), next=next_at, uncertain=flag, count=counted)
        await query(
            url, NEW_INSERT, **{k: v for k, v in values.items() if k in NEW_INSERT._bindparams}
        )
    refused = await migrate(url, "downgrade", BASE)
    assert refused.returncode != 0
    assert "降级" in refused.stdout + refused.stderr
    (version,) = await query(url, "SELECT version_num FROM alembic_version")
    assert version.version_num == HEAD
    kept = await query(
        url,
        "SELECT operation_id, status, delivery_uncertain, not_received_count "
        "FROM reliable_message WHERE target = :target ORDER BY operation_id",
        target=target,
    )
    assert {
        r.operation_id: (r.status, bool(r.delivery_uncertain), r.not_received_count) for r in kept
    } == {oid: (state, flag, counted) for oid, state, flag, counted, _ in rows}
    await query(
        url,
        "DELETE FROM reliable_message WHERE target = :t AND operation_id = :o",
        t=target,
        o=blocker,
    )
    await query(
        url,
        "UPDATE reliable_message SET delivery_uncertain = :f WHERE target = :t",
        t=target,
        f=False,
    )
    await query(
        url, "UPDATE reliable_message SET not_received_count = 0 WHERE target = :t", t=target
    )
    assert (await migrate(url, "downgrade", BASE)).returncode == 0
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            columns = {
                c["name"]: c
                for c in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("reliable_message")
                )
            }
            checks = await connection.run_sync(
                lambda sync: inspect(sync).get_check_constraints("reliable_message")
            )
    finally:
        await engine.dispose()
    assert "delivery_uncertain" not in columns and "not_received_count" not in columns
    assert columns["next_attempt_at"]["nullable"] is False
    assert all("STALLED" not in check["sqltext"] for check in checks)
    survivors = await query(
        url,
        "SELECT operation_id FROM reliable_message WHERE target = :t ORDER BY operation_id",
        t=target,
    )
    assert [r.operation_id for r in survivors] == ["clean", "done"]
