"""Delivery facts notify only after a successful, current completion transaction."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.models import ReliableMessage
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus


async def claim_message(storage, callback):
    store = MessageStore(storage.factory, clock=storage.clock, on_result=callback)
    async with storage.factory.begin() as session:
        reference = await storage.outbox.enqueue(
            session, storage.draft(business_key="opaque:original")
        )
    claims = await store.claim(1, storage.targets)
    return store, reference, claims[0]


async def test_saved_result_notifies_operation_once(storage, delivered):
    published = []
    store, reference, claim = await claim_message(storage, lambda *args: published.append(args))
    assert await store.finish(claim, delivered)
    async with storage.factory() as reader:
        row = await reader.get(ReliableMessage, reference.message_id)
        assert row.status == "DONE" and row.response_raw == b"ok"
    assert published == [("opaque", "opaque:original")]
    assert not await store.finish(claim, delivered)
    assert len(published) == 1


async def test_result_commit_failure_does_not_publish(storage, delivered):
    published = []
    store, reference, claim = await claim_message(storage, lambda *args: published.append(args))

    def failing_factory():
        session = storage.factory()

        def fail(_):
            raise ValueError("commit rejected")

        event.listen(session.sync_session, "before_commit", fail, once=True)
        return session

    store.session_factory = failing_factory
    with pytest.raises(ValueError, match="commit rejected"):
        await store.finish(claim, delivered)
    assert published == []
    async with storage.factory() as reader:
        row = await reader.get(ReliableMessage, reference.message_id)
        assert row.status == "SENDING" and row.response_raw is None


async def test_application_sender_publishes_after_result_is_visible(pg_rig, pg_url):
    from app.http_client import DeliveryState, HttpResult
    from app.reliable.config import ReliableConfig
    from app.reliable_runtime import reliable_lifespan

    published = []
    notified = asyncio.Event()

    def notice(*args):
        published.append(args)
        notified.set()

    class Peer:
        async def send(self, request):
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    async with reliable_lifespan(
        ReliableConfig(pg_rig.targets),
        pg_rig.adapters,
        database_url=pg_url,
        client_factory=lambda _: Peer(),
        on_result=notice,
    ):
        async with pg_rig.factory.begin() as session:
            reference = await pg_rig.outbox.enqueue(session, pg_rig.draft())
        async with asyncio.timeout(5):
            await notified.wait()
        async with pg_rig.factory() as reader:
            row = await reader.get(ReliableMessage, reference.message_id)
            assert row.status == "DONE" and row.response_raw == b"ok"
        assert published == [("opaque", None)]


async def test_publish_failure_does_not_undo_saved_result(storage, delivered):
    def unavailable(*args):
        raise RuntimeError("broker unavailable")

    store, reference, claim = await claim_message(storage, unavailable)
    assert await store.finish(claim, delivered)
    async with storage.factory() as reader:
        row = await reader.get(ReliableMessage, reference.message_id)
        assert row.status == "DONE" and row.response_raw == b"ok"


async def test_scheduled_retry_does_not_publish_a_final_result(storage):
    published = []
    store, _, claim = await claim_message(storage, lambda *args: published.append(args))
    assert await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.PENDING, next_attempt_at=storage.clock() + timedelta(seconds=1)
        ),
    )
    assert published == []


async def test_lost_lease_cannot_publish_old_completion(storage, delivered):
    published = []
    store, reference, claim = await claim_message(storage, lambda *args: published.append(args))
    async with storage.factory.begin() as session:
        row = await session.get(ReliableMessage, reference.message_id)
        row.lease_id = "replacement-lease"
    assert not await store.finish(claim, delivered)
    assert published == []
    async with storage.factory() as session:
        row = await session.get(ReliableMessage, reference.message_id)
        assert row.status == "SENDING" and row.response_raw is None


@pytest.mark.parametrize("transition", ["pending", "lease_recovery", "lease_release"])
async def test_committed_expiry_notifies_original_association_once(storage, transition, delivered):
    notices = []
    store = MessageStore(
        storage.factory, clock=storage.clock, on_result=lambda *args: notices.append(args)
    )
    async with storage.factory.begin() as session:
        ref = await storage.outbox.enqueue(
            session, storage.draft(ttl=timedelta(seconds=1), business_key="opaque:deadline")
        )
    claim = (await store.claim(1, storage.targets))[0] if transition != "pending" else None
    storage.clock.advance(100)

    async def expire():
        if transition == "pending":
            return await store.expire_pending(10)
        if transition == "lease_recovery":
            return await store.recover_expired(10, storage.targets)
        return await store.release(claim, storage.targets)

    assert await expire() == 1 and notices == [("opaque", "opaque:deadline")]
    assert await expire() == 0
    if claim is not None:
        assert not await store.finish(claim, delivered)
    assert notices == [("opaque", "opaque:deadline")]
    async with storage.factory() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert row.status == "EXPIRED" and row.response_raw is None
        assert row.business_key == "opaque:deadline"
        if claim is not None:
            assert row.delivery_uncertain
