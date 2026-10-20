from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import ReliableMessage


@pytest.mark.parametrize("status", ["DONE", "FAILED", "EXPIRED"])
async def test_terminal_retention_exact_boundary_and_reenqueue(storage, status):
    from app.reliable import DuplicateMessage, InvalidMessage
    from app.tasks.reliable_tasks import run_cleanup

    rig = storage
    cutoff = rig.clock() - timedelta(days=90)
    async with rig.factory() as session:
        old = await rig.outbox.enqueue(session, rig.draft("old"))
        equal = await rig.outbox.enqueue(session, rig.draft("equal"))
        new = await rig.outbox.enqueue(session, rig.draft("new"))
        opened = await rig.outbox.enqueue(session, rig.draft("open"))
        for result, updated in [
            (old, cutoff - timedelta(microseconds=1)),
            (equal, cutoff),
            (new, rig.clock()),
            (opened, cutoff - timedelta(days=1)),
        ]:
            await session.execute(
                update(ReliableMessage)
                .where(ReliableMessage.id == result.message_id)
                .values(
                    status="PENDING" if result == opened else status,
                    created_at=cutoff - timedelta(days=1),
                    updated_at=updated,
                )
            )
        await session.commit()
    rig.targets[rig.target.name] = replace(rig.target, max_body_bytes=1)
    async with rig.factory() as session:
        assert (await rig.outbox.enqueue(session, rig.draft("old"))).message_id == old.message_id
        with pytest.raises(DuplicateMessage):
            await rig.outbox.enqueue(session, rig.draft("old", request_raw=b"different"))
    assert await run_cleanup(rig.factory, 90, now=rig.clock()) == 1
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert {row.id for row in rows} == {equal.message_id, new.message_id, opened.message_id}
        with pytest.raises(InvalidMessage):
            await rig.outbox.enqueue(session, rig.draft("old"))
        result = await rig.outbox.enqueue(session, rig.draft("old", request_raw=b"x"))
        await session.commit()
        assert result.message_id != old.message_id


async def test_cleanup_deletes_in_bounded_batches(rig):
    from app.tasks.reliable_tasks import run_cleanup

    async with rig.factory() as session:
        ids = [(await rig.outbox.enqueue(session, rig.draft(f"m{n}"))).message_id for n in range(5)]
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id.in_(ids))
            .values(status="DONE", updated_at=rig.clock() - timedelta(days=100))
        )
        await session.commit()
    assert await run_cleanup(rig.factory, 90, now=rig.clock(), batch=2) == 5
    async with rig.factory() as session:
        assert not list(await session.scalars(select(ReliableMessage.id)))
