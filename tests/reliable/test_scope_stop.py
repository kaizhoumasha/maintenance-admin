import pytest

from app.models import ReliableMessage
from app.reliable import ScopeRef


async def test_unscoped_delivery_keeps_scope_evidence_empty(storage, delivered):
    rig = storage
    async with rig.factory.begin() as session:
        queued = await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, rig.targets)
    assert await rig.store.before_send(claim, rig.target)
    assert await rig.store.finish(claim, delivered)
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, queued.message_id)
        assert row.status == "DONE" and row.response_raw == b"ok"
        assert row.scope_owner is None and row.dispatch_started_at is None


@pytest.mark.parametrize("gate_first", [False, True])
async def test_scope_stop_fences_claim_and_preserves_dispatch_fact(storage, delivered, gate_first):
    rig = storage
    scope = ScopeRef("owner", "TASK", "original")
    async with rig.factory.begin() as session:
        result = await rig.outbox.enqueue(session, rig.draft(scope=scope))
        await rig.outbox.enqueue(session, rig.draft("fact"))
    claims = await rig.store.claim(10, rig.targets)
    claim = next(item for item in claims if item.message_id == result.message_id)
    if gate_first:
        assert await rig.store.before_send(claim, rig.target)
    async with rig.factory.begin() as session:
        stopped = await rig.outbox.stop_scope(session, scope, reason="TASK_COMPLETED")
        assert len(stopped) == 1
        assert stopped[0].dispatch == ("MAY_HAVE_DISPATCHED" if gate_first else "NEVER_DISPATCHED")
    assert await rig.store.before_send(claim, rig.target) is None
    assert not await rig.store.finish(claim, delivered)
    assert not await rig.store.release(claim, rig.targets)
    async with rig.factory.begin() as session:
        assert not await rig.outbox.resume(session, claim.message.target, "opaque", "id")
        assert not await rig.outbox.settle(session, claim.message.target, "opaque", "id")
        row = await session.get(ReliableMessage, result.message_id)
        assert row.status == "ABANDONED" and row.next_attempt_at is None
        assert row.lease_id is None and row.request_raw == claim.message.request_raw
        assert bool(row.dispatch_started_at) == gate_first
    rig.clock.advance(3600)
    await rig.store.recover_expired(10, rig.targets)
    assert all(
        item.message_id != result.message_id for item in await rig.store.claim(10, rig.targets)
    )


async def test_stop_rollback_preserves_original_claim(storage):
    rig = storage
    scope = ScopeRef("owner", "TASK", "original")
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft(scope=scope))
    (claim,) = await rig.store.claim(1, rig.targets)
    async with rig.factory() as session:
        await rig.outbox.stop_scope(session, scope, reason="TASK_CANCELLED")
        await session.rollback()
    assert await rig.store.before_send(claim, rig.target)


async def test_expired_claim_without_gate_is_still_never_dispatched(storage):
    rig = storage
    scope = ScopeRef("owner", "TASK", "original")
    async with rig.factory.begin() as session:
        result = await rig.outbox.enqueue(session, rig.draft(scope=scope))
    (claim,) = await rig.store.claim(1, rig.targets)
    rig.clock.advance(60)
    assert await rig.store.recover_expired(10, rig.targets) == 1
    async with rig.factory.begin() as session:
        row = await session.get(ReliableMessage, result.message_id)
        assert row.attempts == 1 and row.last_attempt_at is not None
        assert row.dispatch_started_at is None and not row.delivery_uncertain
        (stopped,) = await rig.outbox.stop_scope(session, scope, reason="TASK_CANCELLED")
        assert stopped.dispatch == "NEVER_DISPATCHED"


@pytest.mark.parametrize("rollback", [False, True])
async def test_committed_stop_and_waiting_send_gate_serialize_on_original_message(pg_rig, rollback):
    import asyncio

    rig = pg_rig
    scope = ScopeRef("owner", "TASK", "original")
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft(scope=scope))
    (claim,) = await rig.store.claim(1, rig.targets)
    async with rig.factory() as session:
        await session.begin()
        await rig.outbox.stop_scope(session, scope, reason="TASK_COMPLETED")
        gate = asyncio.create_task(rig.store.before_send(claim, rig.target))
        try:
            await asyncio.sleep(0.05)
            assert not gate.done()
            if rollback:
                await session.rollback()
            else:
                await session.commit()
            granted = await asyncio.wait_for(gate, 3)
            assert bool(granted) == rollback
        finally:
            if not gate.done():
                gate.cancel()
