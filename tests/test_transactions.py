"""Committed facts survive a failed wakeup; rolled-back work never wakes a consumer."""

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select

from app.core.identifiers import uuid7


@pytest.fixture
async def transaction_store(callback_db):
    table = Table(
        "commit_notice_" + uuid7().hex, MetaData(), Column("id", Integer, primary_key=True)
    )
    engine = callback_db.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(table.create)
    try:
        yield callback_db, table
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(table.drop)


async def test_notification_waits_for_outer_commit(transaction_store):
    from app.transactions import after_commit

    factory, table = transaction_store
    published = []
    async with factory() as session, session.begin():
        await session.execute(table.insert().values(id=1))
        after_commit(session, lambda: published.append(1))
        assert published == []
    async with factory() as reader:
        assert (await reader.scalars(select(table.c.id))).all() == published == [1]


async def test_outer_rollback_drops_notification_and_fact(transaction_store):
    from app.transactions import after_commit

    factory, table = transaction_store
    published = []
    async with factory() as session:
        with pytest.raises(ValueError):
            async with session.begin():
                await session.execute(table.insert().values(id=1))
                after_commit(session, lambda: published.append(1))
                raise ValueError("rollback")
        assert (await session.scalars(select(table.c.id))).all() == published == []


async def test_savepoint_release_waits_for_outer_commit(transaction_store):
    from app.transactions import after_commit

    factory, _ = transaction_store
    published = []
    async with factory() as session, session.begin():
        after_commit(session, lambda: published.append("outer"))
        async with session.begin_nested():
            after_commit(session, lambda: published.append("inner"))
        assert published == []
    assert published == ["outer", "inner"]


async def test_outer_rollback_discards_released_savepoint_notification(transaction_store):
    from app.transactions import after_commit

    factory, _ = transaction_store
    published = []
    async with factory() as session:
        with pytest.raises(ValueError):
            async with session.begin():
                async with session.begin_nested():
                    after_commit(session, lambda: published.append("inner"))
                raise ValueError("outer rollback")
    assert published == []


async def test_savepoint_rollback_discards_only_its_notifications(transaction_store):
    from app.transactions import after_commit

    factory, _ = transaction_store
    published = []
    async with factory() as session, session.begin():
        after_commit(session, lambda: published.append("outer"))
        with pytest.raises(ValueError):
            async with session.begin_nested():
                after_commit(session, lambda: published.append("discard"))
                raise ValueError("savepoint rollback")
        after_commit(session, lambda: published.append("after"))
    assert published == ["outer", "after"]


async def test_uncommitted_close_does_not_leak_into_reused_session(transaction_store):
    from app.transactions import after_commit

    factory, table = transaction_store
    published = []
    session = factory()
    await session.begin()
    await session.execute(table.insert().values(id=1))
    after_commit(session, lambda: published.append(1))
    await session.close()
    async with session, session.begin():
        await session.execute(table.insert().values(id=2))
        after_commit(session, lambda: published.append(2))
    async with factory() as reader:
        assert (await reader.scalars(select(table.c.id))).all() == published == [2]


async def test_notification_failure_preserves_commit_and_other_notifications(
    transaction_store, caplog
):
    from app.transactions import after_commit

    factory, table = transaction_store
    published = []

    def unavailable():
        raise RuntimeError("sensitive-broker-credentials")

    async with factory() as session, session.begin():
        await session.execute(table.insert().values(id=1))
        after_commit(session, unavailable)
        after_commit(session, lambda: published.append(1))
    async with factory() as reader:
        assert (await reader.scalars(select(table.c.id))).all() == published == [1]
    assert "sensitive-broker-credentials" not in caplog.text


async def test_notifications_are_scoped_to_each_transaction(transaction_store):
    from app.transactions import after_commit

    factory, _ = transaction_store
    published = []
    async with factory() as session:
        for identity in (1, 2):
            async with session.begin():
                after_commit(session, lambda identity=identity: published.append(identity))
    assert published == [1, 2]


async def test_notification_requires_an_active_transaction(transaction_store):
    from app.transactions import after_commit

    factory, _ = transaction_store
    async with factory() as session:
        with pytest.raises(RuntimeError, match="transaction"):
            after_commit(session, lambda: None)
