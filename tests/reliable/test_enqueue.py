from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.reliable import DuplicateMessage, InvalidMessage


async def test_caller_transaction_and_duplicate_contract(rig):
    from app.models.reliable import ReliableMessage

    async with rig.factory() as session:
        await rig.outbox.enqueue(session, rig.draft())
        await session.rollback()
    async with rig.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0
        original = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(seconds=5)))
        duplicate = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(seconds=100)))
        assert duplicate == original
        with pytest.raises(DuplicateMessage):
            await rig.outbox.enqueue(session, rig.draft(request_raw=b"different"))
        await rig.outbox.enqueue(session, rig.draft("another"))
        await session.commit()
    assert len(rig.adapter.builds) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "bad"},
        {"path": "//bad.test"},
        {"query": (("x", "\n"),)},
        {"headers": (("Host", "bad"),)},
        {"body": "text"},
        {"body": b"changed"},
    ],
)
async def test_invalid_adapter_request_never_persists(rig, changes):
    from app.models.reliable import ReliableMessage

    rig.adapter.changes = changes
    async with rig.factory() as session:
        with pytest.raises(InvalidMessage):
            await rig.outbox.enqueue(session, rig.draft())
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_existing_identity_precedes_current_adapter_and_body_limit(rig):
    async with rig.factory() as session:
        original = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    rig.adapters.clear()
    async with rig.factory() as session:
        assert await rig.outbox.enqueue(session, rig.draft()) == original
        with pytest.raises(DuplicateMessage):
            await rig.outbox.enqueue(session, rig.draft(request_raw=b"different"))
        with pytest.raises(InvalidMessage):
            await rig.outbox.enqueue(session, rig.draft("new"))
