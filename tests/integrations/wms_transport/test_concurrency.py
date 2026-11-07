import asyncio

import pytest
from sqlalchemy import func, select, text

from app.integrations import history as history_records
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


@pytest.mark.parametrize(
    "original_owner,new_owner",
    [(None, "first"), ("first", None), ("first", "second"), ("first", "first")],
)
async def test_concurrent_transport_registration_preserves_winner_owner(
    pg_transport_rig, original_owner, new_owner, wait_for_lock
):
    from app.core.identifiers import uuid7
    from app.integrations.wms_transport import (
        IdempotencyConflict,
        Rack,
        RackPosition,
        TransportCaller,
        TransportService,
    )

    rig = pg_transport_rig
    first_service = TransportService(
        rig.outbox, target=rig.target.name, clock=rig.clock, owner_domain=original_owner
    )
    second_service = TransportService(
        rig.outbox, target=rig.target.name, clock=rig.clock, owner_domain=new_owner
    )
    arguments = dict(
        client_request_id=str(uuid7()),
        caller=TransportCaller(rig.target.name),
        rack_id="rack",
        source=Rack("rack"),
        target=RackPosition("TARGET"),
    )
    async with rig.factory() as first:
        original = await first_service.move_rack(first, **arguments)

        async def compete():
            async with rig.factory.begin() as second:
                return await second_service.move_rack(second, **arguments)

        pending = asyncio.create_task(compete())
        try:
            await wait_for_lock(pending)
            await first.commit()
            async with asyncio.timeout(5):
                if original_owner == new_owner:
                    assert await pending == original
                else:
                    with pytest.raises(IdempotencyConflict, match="owner|business"):
                        await pending
            async with rig.factory() as session:
                task = await session.scalar(
                    select(TransportTask).where(
                        TransportTask.transport_task_id == original.transport_task_id
                    )
                )
                assert task.owner_domain == original_owner
        finally:
            await first.rollback()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_callback_can_settle_while_evidence_waits_on_task(
    pg_transport_rig, wait_for_lock, rack_failure_callback
):
    import json

    from app.core.identifiers import uuid7
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.wms_transport.acceptance import submission_observer
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore
    from app.reliable.testing import ScriptedHttpClient

    rig = pg_transport_rig
    caller = TransportCaller(rig.target.name)
    async with rig.factory.begin() as session:
        handle = await rig.service.move_rack(
            session,
            client_request_id=str(uuid7()),
            caller=caller,
            rack_id="R1",
            source=Rack("R1"),
            target=RackPosition("DEST"),
        )
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
    raw = json.dumps(
        dict(
            operation_id=message.operation_id,
            code="RECEIVED",
            timestamp=1,
            data=dict(transport_task_id=handle.transport_task_id),
        )
    ).encode()
    peer = ScriptedHttpClient(HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw))
    sender = Sender(
        MessageStore(rig.factory, clock=rig.clock, records=history_records),
        rig.outbox.targets,
        rig.adapters,
        {rig.target.name: peer},
        clock=rig.clock,
        observe_delivered=submission_observer(rig.outbox.targets, rig.factory, lambda _: None),
    )
    try:
        async with rig.factory.begin() as callback:
            await callback.scalar(
                select(TransportTask)
                .where(TransportTask.transport_task_id == handle.transport_task_id)
                .with_for_update()
            )
            assert await sender.tick() == 1
            (delivery,) = sender.inflight
            await wait_for_lock(delivery)
            assert not delivery.done()
            await asyncio.wait_for(rack_failure_callback(rig, callback, handle, "R1"), 5)
        await asyncio.wait_for(delivery, 5)
        async with rig.factory() as session:
            result = await rig.service.get_submission_acceptance(
                session,
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=caller,
            )
            assert result.state == "ACCEPTED" and result.basis.message_id == message.id
            final = await session.get(ReliableMessage, message.id)
            assert (
                final.status == "DONE"
                and final.terminal_response_record_id is None
                and len(peer.requests) == 1
            )
    finally:
        await sender.aclose()
