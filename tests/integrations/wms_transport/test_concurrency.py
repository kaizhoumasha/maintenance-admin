import asyncio

import pytest
from sqlalchemy import func, select, text

from app.integrations.wms_transport import (
    IdempotencyConflict,
    Rack,
    RackPosition,
    TransportCaller,
)
from app.models import Menu, ReliableMessage, TransportTask


@pytest.mark.parametrize("commit_first", [True, False])
@pytest.mark.parametrize("changed_input", [True, False])
async def test_concurrent_reservation_only_enqueues_the_committed_winner(
    pg_transport_rig, wait_for_lock, commit_first, changed_input
):
    rig = pg_transport_rig
    request = dict(
        client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
        caller=TransportCaller(rig.target.name),
        rack_id="rack",
        source=Rack("rack"),
        target=RackPosition("destination"),
    )
    async with rig.factory() as first, rig.factory() as second:
        original = await rig.service.move_rack(first, **request)
        second.add(Menu(name=rig.target.name, path="/caller", title="Caller", component="Page"))
        await second.flush()
        pid = await second.scalar(text("SELECT pg_backend_pid()"))
        competing = request | ({"target": RackPosition("other")} if changed_input else {})
        pending = asyncio.create_task(rig.service.move_rack(second, **competing))
        try:
            await wait_for_lock(pending, pid)
            assert not pending.done(), "competitor must wait for the uncommitted reservation"
            await first.commit() if commit_first else await first.rollback()
            if commit_first and changed_input:
                with pytest.raises(IdempotencyConflict):
                    await asyncio.wait_for(pending, 5)
            else:
                winner = await asyncio.wait_for(pending, 5)
                assert (winner == original) == commit_first
            await second.commit()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    async with rig.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.workline_id == rig.target.name)
        )
        messages = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert len(messages) == 1 and messages[0].operation_id == task.operation_id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TransportTask)
                .where(TransportTask.workline_id == rig.target.name)
            )
            == 1
        )
        assert await session.scalar(select(Menu.id).where(Menu.name == rig.target.name)) is not None
