"""Task-owned connections may be reused, but engines never cross event loops."""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from celery import Celery
from sqlalchemy import event, text

from app.tasks._db import run_with_factory


def test_task_connections_are_reused_within_one_loop_and_disposed(tmp_path):
    connections, loops, disposed = [], [], []

    async def work(factory):
        engine = factory.kw["bind"]
        loops.append(asyncio.get_running_loop())
        event.listen(engine.sync_engine, "engine_disposed", lambda engine: disposed.append(engine))
        for _ in range(3):
            async with factory() as session:
                raw = await (await session.connection()).get_raw_connection()
                connections.append(raw.driver_connection)
                assert await session.scalar(text("SELECT 1")) == 1
        return 3

    url = f"sqlite+aiosqlite:///{tmp_path / 'task.db'}"
    assert run_with_factory(url, work) == run_with_factory(url, work) == 3
    assert all(connection is connections[0] for connection in connections[:3])
    assert all(connection is connections[3] for connection in connections[3:])
    assert connections[0] is not connections[3]
    assert len(disposed) == 2 and disposed[0] is not disposed[1]
    assert loops[0] is not loops[1] and all(loop.is_closed() for loop in loops)


def test_task_failure_still_disposes_its_engine(tmp_path):
    disposed = []

    async def work(factory):
        event.listen(factory.kw["bind"].sync_engine, "engine_disposed", disposed.append)
        async with factory() as session:
            assert await session.scalar(text("SELECT 1")) == 1
            raise ValueError("controlled task failure")

    with pytest.raises(ValueError, match="controlled task failure"):
        run_with_factory(f"sqlite+aiosqlite:///{tmp_path / 'task.db'}", work)
    assert len(disposed) == 1


@pytest.mark.parametrize("fenced", [False, True])
async def test_worker_rejects_wrong_authority_before_consuming(tmp_path, real_redis, fenced):
    redis_url = os.environ["TEST_REDIS_URL"]
    queue = f"authority-{uuid4().hex}"
    producer = Celery("authority-test", broker=redis_url, set_as_current=False)
    # Runtime authority requires reconciliation. An empty SQLite store has neither the
    # final revision nor a fence, and stays usable in legacy mode.
    producer.send_task("users.welcome", args=["fixture"], queue=queue)
    database = tmp_path / "authority.db"
    if fenced:
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE integration_exchange (id INTEGER);
                CREATE TRIGGER integration_exchange_runtime_fence_insert
                BEFORE INSERT ON integration_exchange
                BEGIN SELECT RAISE(ABORT, 'integration history writes retired'); END;
            """)
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite+aiosqlite:///{database}",
        REDIS_URL=redis_url,
        INTEGRATION_RUNTIME_AUTHORITY="false" if fenced else "true",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "celery",
        "-A",
        "app.tasks.celery_app",
        "worker",
        "--pool=solo",
        "-l",
        "INFO",
        "-Q",
        queue,
        "--without-gossip",
        "--without-mingle",
        cwd=Path(__file__).parents[2],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output = b""
        async with asyncio.timeout(15):
            while line := await process.stdout.readline():
                output += line
                assert b" ready." not in line, output.decode()
        assert await process.wait() != 0, output.decode()
        expected = (
            b"database requires INTEGRATION_RUNTIME_AUTHORITY=true"
            if fenced
            else b"integration runtime authority requires successful reconciliation"
        )
        assert expected in output, output.decode()
        assert await real_redis.llen(queue) == 1
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await real_redis.delete(queue)
        producer.close()
