from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import ReliableMessage
from tests.helpers import message_history_view


async def test_open_commit_failure_prevents_http_for_both_modes(rig, delivered, monkeypatch):
    import pytest
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from app.integrations import history
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient

    await enqueue(rig)
    (claim,) = await rig.store.claim(1, rig.targets)
    original = history.opened

    async def opened(session, **kwargs):
        result = await original(session, **kwargs)
        session.info["fail_history_commit"] = True
        return result

    def fail_commit(session):
        if session.info.get("fail_history_commit"):
            raise OperationalError(None, None, RuntimeError("commit unavailable"))

    monkeypatch.setattr(history, "opened", opened)
    event.listen(rig.factory.class_.sync_session_class, "before_commit", fail_commit)
    client = ScriptedHttpClient(delivered.result)
    try:
        with pytest.raises(history.EvidencePersistenceError):
            await send_one(
                claim,
                store=rig.store,
                targets=rig.targets,
                adapters=rig.adapters,
                clients={rig.target.name: client},
                clock=rig.clock,
                uniform=lambda low, high: high,
            )
    finally:
        event.remove(rig.factory.class_.sync_session_class, "before_commit", fail_commit)
    assert client.requests == []
    async with rig.factory() as session:
        row = await message_history_view(
            session, await session.get(ReliableMessage, claim.message_id)
        )
        assert row.status == "SENDING"
        assert row.lease_id == claim.lease_id
        assert row.fail_reason is None


async def test_finish_storage_failure_keeps_original_recovery(rig, delivered, monkeypatch):
    import pytest

    from app.integrations import history

    await enqueue(rig)
    (claim,) = await rig.store.claim(1, rig.targets)

    async def unavailable(*args, **kwargs):
        raise history.EvidencePersistenceError("evidence unavailable")

    monkeypatch.setattr(history, "finished", unavailable)
    with pytest.raises(history.EvidencePersistenceError):
        await rig.store.finish(claim, delivered)
    async with rig.factory() as session:
        row = await message_history_view(
            session, await session.get(ReliableMessage, claim.message_id)
        )
        assert row.status == "SENDING"
        assert row.lease_id == claim.lease_id
        assert row.operation_id == "id"
        assert row.response_raw is None


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
        row = await message_history_view(session, await session.get(ReliableMessage, message_id))
        assert row.status == "SENDING" and row.lease_id == current.lease_id
        assert row.attempts == 1
    assert await rig.store.finish(current, delivered)
    assert not await rig.store.finish(current, delivered)
    assert not await rig.store.release(original, rig.targets)
    assert not await rig.store.release(current, rig.targets)
    async with rig.factory() as session:
        row = await message_history_view(session, await session.get(ReliableMessage, message_id))
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
        row = await message_history_view(session, await session.get(ReliableMessage, sending_id))
        assert row.status == MessageStatus.EXPIRED
        assert row.attempts == 1
        assert row.last_http_status is None and row.last_response_raw is None


async def test_unknown_naive_clock_is_rejected(storage):
    import pytest

    rig = storage
    rig.store.clock = lambda: rig.clock().replace(tzinfo=None)
    with pytest.raises(ValueError, match="naive"):
        await rig.store.claim(1, rig.targets)


@pytest.mark.parametrize("recovery", ["release", "expired"])
async def test_interrupted_attempt_clears_previous_response_but_keeps_history(storage, recovery):
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.history import read_bodies
    from app.reliable import Verdict, VerdictKind
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus
    from app.services.integration_history_service import get_exchange

    rig = storage
    message_id = await enqueue(rig)
    (first,) = await rig.store.claim(1, rig.targets)
    assert await rig.store.finish(
        first,
        AttemptOutcome(
            MessageStatus.PENDING,
            next_attempt_at=rig.clock() + timedelta(seconds=1),
            result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), b"busy"),
            verdict=Verdict(VerdictKind.NOT_RECEIVED),
        ),
    )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message_id)
        previous_response = row.last_response_record_id
        assert previous_response is not None
    rig.clock.advance(1)
    (second,) = await rig.store.claim(1, rig.targets)
    if recovery == "release":
        assert await rig.store.release(second, rig.targets)
    else:
        rig.clock.advance(15)
        assert await rig.store.recover_expired(1, rig.targets) == 1
    async with rig.factory() as session:
        row = await message_history_view(session, await session.get(ReliableMessage, message_id))
        assert row.last_http_status is None
        assert row.last_response_raw is None
        summary = await get_exchange(session, second.message.exchange_id)
        assert summary["protocol_result"]["source_record_id"] is None
        original = (await read_bodies(session, record_ids=(previous_response,)))[previous_response]
        assert original.raw_body == b"busy"


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
        row = await message_history_view(session, await session.get(ReliableMessage, message_id))
        assert row.status == "PENDING" and row.attempts == 0
    assert len(await rig.store.claim(1, rig.targets)) == 1
