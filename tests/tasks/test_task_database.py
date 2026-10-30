"""Task-owned connections may be reused, but engines never cross event loops."""

import asyncio

import pytest
from sqlalchemy import event, text

from app.tasks._db import run_with_factory


def test_task_connections_are_reused_within_one_loop_and_disposed(tmp_path):
    connections, loops, disposed = [], [], []

    async def work(factory):
        engine = factory.kw["bind"]
        loops.append(asyncio.get_running_loop())
        event.listen(
            engine.sync_engine, "connect", lambda connection, _: connections.append(connection)
        )
        event.listen(engine.sync_engine, "engine_disposed", lambda engine: disposed.append(engine))
        for _ in range(3):
            async with factory() as session:
                assert await session.scalar(text("SELECT 1")) == 1
        return 3

    url = f"sqlite+aiosqlite:///{tmp_path / 'task.db'}"
    assert run_with_factory(url, work) == run_with_factory(url, work) == 3
    assert len(connections) == 2
    assert connections[0] is not connections[1]
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
