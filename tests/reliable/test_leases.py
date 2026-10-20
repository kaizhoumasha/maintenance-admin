from datetime import timedelta

from sqlalchemy import select, update

from app.models import ReliableMessage


async def enqueue(rig, identity="id", **kwargs):
    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft(identity, **kwargs))
        await session.commit()
        return result.message_id


async def test_lease_fences_late_result_and_counts_once(storage, delivered):
    rig = storage
    message_id = await enqueue(rig)
    (original,) = await rig.store.claim(1, rig.targets)
    assert original.message.created_at.tzinfo is not None
    assert original.lease_until.tzinfo is not None
    assert not await rig.store.claim(1, rig.targets)
    rig.clock.advance(15)
    assert await rig.store.recover_expired(1, rig.targets) == 1
    assert not await rig.store.finish(original, delivered)
    rig.clock.advance(1)
    (current,) = await rig.store.claim(1, rig.targets)
    assert current.lease_id != original.lease_id
    assert not await rig.store.release(original, rig.targets)
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message_id)
        assert row.status == "SENDING" and row.lease_id == current.lease_id
        assert row.attempts == 1
    assert await rig.store.finish(current, delivered)
    assert not await rig.store.finish(current, delivered)
    assert not await rig.store.release(original, rig.targets)
    assert not await rig.store.release(current, rig.targets)
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message_id)
        assert row.status == "DONE" and row.response_raw == b"ok"
        assert row.attempts == 2
        assert row.lease_id is None


async def test_invalid_terminal_outcome_does_not_fabricate_success(rig):
    import pytest

    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus

    await enqueue(rig)
    (claim,) = await rig.store.claim(1, rig.targets)
    with pytest.raises(ValueError):
        await rig.store.finish(claim, AttemptOutcome(MessageStatus.DONE))
    with pytest.raises(ValueError):
        await rig.store.finish(claim, AttemptOutcome(MessageStatus.EXPIRED))


async def test_pending_expiry_is_independent_of_retry_due_and_sending(storage):
    from app.reliable.types import MessageStatus

    rig = storage
    pending_id = await enqueue(rig, "pending", ttl=timedelta(microseconds=1))
    sending_id = await enqueue(rig, "sending", ttl=timedelta(microseconds=1))
    async with rig.factory() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == pending_id)
            .values(next_attempt_at=rig.clock() + timedelta(days=1))
        )
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    assert claim.message_id == sending_id
    assert claim.message.expires_at == rig.clock() + timedelta(microseconds=1)
    assert await rig.store.expire_pending(4) == 0
    rig.clock.advance(0.000001)
    assert await rig.store.expire_pending(1) == 1
    assert await rig.store.expire_pending(1) == 0
    async with rig.factory() as session:
        rows = list(await session.scalars(select(ReliableMessage).order_by(ReliableMessage.id)))
        assert [row.status for row in rows] == [MessageStatus.EXPIRED, MessageStatus.SENDING]
        assert rows[0].attempts == 0
    rig.clock.advance(15)
    assert await rig.store.recover_expired(1, rig.targets) == 1
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, sending_id)
        assert row.status == MessageStatus.EXPIRED
        assert row.attempts == 1
        assert row.last_http_status is None and row.last_response_raw is None


async def test_unknown_naive_clock_is_rejected(storage):
    import pytest

    rig = storage
    rig.store.clock = lambda: rig.clock().replace(tzinfo=None)
    with pytest.raises(ValueError, match="naive"):
        await rig.store.claim(1, rig.targets)


async def test_recovery_and_expiry_logs_survive_application_formatter(rig, caplog):
    import logging

    from app.core.logger import DevFormatter, TraceIdFilter

    caplog.set_level(logging.INFO)
    await enqueue(rig, "recover")
    await enqueue(rig, "expire", ttl=timedelta(0))
    await rig.store.claim(1, rig.targets)
    rig.clock.advance(15)
    assert await rig.store.recover_expired(1, rig.targets) == 1
    assert await rig.store.expire_pending(1) == 1
    transitions = [record for record in caplog.records if getattr(record, "from_status", None)]
    assert {record.to_status for record in transitions} == {"SENDING", "PENDING", "EXPIRED"}
    for record in transitions:
        TraceIdFilter().filter(record)
        line = DevFormatter(False).format(record)
        assert record.operation_id in line and "peer" in line and "opaque" in line


async def test_claim_leaves_unserved_targets_pending(rig):
    message_id = await enqueue(rig)
    assert not await rig.store.claim(1, {})
    assert not await rig.store.claim(1, {"other": rig.target})
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message_id)
        assert row.status == "PENDING" and row.attempts == 0
    assert len(await rig.store.claim(1, rig.targets)) == 1
