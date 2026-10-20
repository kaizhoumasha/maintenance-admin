import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.http_client import DeliveryState, HttpResult
from app.models import ReliableMessage


@pytest.mark.parametrize("claim_batch_size", [1, 4])
@pytest.mark.parametrize("local_error", [False, True])
async def test_completed_sends_refill_slots_without_polling_delay(
    pg_rig, claim_batch_size, local_error
):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = pg_rig
    full, parked, release, refilled = (asyncio.Event() for _ in range(4))
    requests = []
    active = maximum = 0

    class Client:
        async def send(self, request):
            nonlocal active, maximum
            identity = dict(request.headers)["X-Operation-Id"]
            requests.append((identity, request.body))
            active += 1
            maximum = max(maximum, active)
            if len(requests) == 4:
                full.set()
            if len(requests) == 6:
                refilled.set()
            try:
                await release.wait()
                if local_error and int(identity) < 4:
                    raise RuntimeError("local send failure")
                return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")
            finally:
                active -= 1

        async def aclose(self):
            pass

    async def wait(seconds):
        parked.set()
        await rig.clock.wait(seconds)

    async with rig.factory() as session:
        for number in range(6):
            await rig.outbox.enqueue(session, rig.draft(str(number)))
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {rig.target.name: Client()},
        clock=rig.clock,
        wait=wait,
        budget=ReliableConfig(rig.targets, claim_batch_size=claim_batch_size),
    )
    runner = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(3):
            await full.wait()
            await parked.wait()
        assert len(requests) == 4 and maximum == 4
        release.set()
        async with asyncio.timeout(3):
            await refilled.wait()
    finally:
        release.set()
        await sender.aclose()
    assert runner.done() and active == 0
    assert len(requests) == 6 and len({identity for identity, _ in requests}) == 6
    assert all(body == b"\x00opaque" for _, body in requests)
    assert rig.clock.monotonic() == 0
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert all(row.attempts == 1 for row in rows)
        assert sum(row.status == "DONE" for row in rows) == (2 if local_error else 6)
        assert sum(row.fail_reason == "LOCAL_ERROR" for row in rows) == (4 if local_error else 0)


async def test_maintenance_batch_is_independent_of_send_claims(storage):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = storage
    async with rig.factory() as session:
        for number in range(9):
            await rig.outbox.enqueue(session, rig.draft(f"sending-{number}"))
        await session.commit()
    assert len(await rig.store.claim(9, rig.targets)) == 9
    async with rig.factory() as session:
        for number in range(9):
            await rig.outbox.enqueue(
                session, rig.draft(f"expired-{number}", ttl=timedelta(seconds=1))
            )
        await session.commit()
    rig.clock.advance(16)
    sender = Sender(rig.store, {}, {}, {}, budget=ReliableConfig({}, claim_batch_size=1))
    try:
        assert await sender.tick() == 0
    finally:
        await sender.aclose()
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert sum(row.status == "EXPIRED" and row.attempts == 0 for row in rows) == 9
        assert sum(row.status == "PENDING" and row.attempts == 1 for row in rows) == 9
        assert all(row.lease_id is None for row in rows)


async def test_maintenance_batches_remain_finite(storage):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = storage
    async with rig.factory() as session:
        for number in range(7):
            await rig.outbox.enqueue(session, rig.draft(f"sending-{number}"))
        await session.commit()
    await rig.store.claim(7, rig.targets)
    async with rig.factory() as session:
        for number in range(7):
            await rig.outbox.enqueue(
                session, rig.draft(f"expired-{number}", ttl=timedelta(seconds=1))
            )
        await session.commit()
    rig.clock.advance(16)
    sender = Sender(
        rig.store,
        {},
        {},
        {},
        budget=ReliableConfig({}, claim_batch_size=1, maintenance_batch_size=5),
    )
    try:
        assert await sender.tick() == 0
        async with rig.factory() as session:
            rows = list(
                await session.scalars(
                    select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                )
            )
            assert sum(row.status == "EXPIRED" for row in rows) == 5
            assert sum(row.status == "SENDING" for row in rows) == 2
            assert sum(row.status == "PENDING" for row in rows) == 7
        assert await sender.tick() == 0
    finally:
        await sender.aclose()
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert sum(row.status == "EXPIRED" for row in rows) == 7
        assert sum(row.status == "PENDING" for row in rows) == 7
        assert all(row.lease_id is None for row in rows)


async def test_idle_poll_expires_messages_without_send_completions(storage):
    from app.reliable.sender import Sender

    rig = storage
    polls = asyncio.Queue()

    async def wait(seconds):
        polls.put_nowait(seconds)
        await rig.clock.wait(seconds)

    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(seconds=1)))
        await session.commit()
    sender = Sender(rig.store, {}, {}, {}, wait=wait)
    runner = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(3):
            assert await polls.get() == 1
            rig.clock.advance(1)
            assert await polls.get() == 1
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, result.message_id)
            assert row.status == "EXPIRED" and row.attempts == 0
    finally:
        await sender.aclose()
    assert runner.done() and not rig.clock.waiters


async def test_slots_do_not_preclaim_and_ttl_runs_while_full(rig):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    started = asyncio.Event()
    unblock = asyncio.Event()

    class Client:
        requests = []

        async def send(self, request):
            self.requests.append(request)
            if len(self.requests) == 4:
                started.set()
            await unblock.wait()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    client = Client()
    async with rig.factory() as session:
        for number in range(5):
            await rig.outbox.enqueue(session, rig.draft(str(number), ttl=timedelta(seconds=1)))
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {"peer": client},
        clock=rig.clock,
        budget=ReliableConfig(rig.targets),
    )
    try:
        assert await sender.tick() == 4
        async with asyncio.timeout(2):
            await started.wait()
        assert await sender.tick() == 0
        async with rig.factory() as session:
            row = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == "4")
            )
            assert row.status == "PENDING" and row.lease_id is None
        rig.clock.advance(1)
        assert await sender.tick() == 0
        async with rig.factory() as session:
            row = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == "4")
            )
            assert row.status == "EXPIRED" and row.attempts == 0
        unblock.set()
    finally:
        unblock.set()
        await sender.aclose()
    assert len(client.requests) == 4


async def test_completions_claim_without_rerunning_maintenance(pg_rig):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = pg_rig
    maintenance = sent = 0
    all_sent = asyncio.Event()
    original = rig.store.expire_pending

    async def spy(limit):
        nonlocal maintenance
        maintenance += 1
        return await original(limit)

    class Client:
        async def send(self, request):
            nonlocal sent
            sent += 1
            if sent == 8:
                all_sent.set()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    rig.store.expire_pending = spy
    async with rig.factory() as session:
        for number in range(8):
            await rig.outbox.enqueue(session, rig.draft(str(number)))
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {rig.target.name: Client()},
        clock=rig.clock,
        wait=rig.clock.wait,
        monotonic=rig.clock.monotonic,
        budget=ReliableConfig(rig.targets, claim_batch_size=1, max_concurrency=2),
    )
    runner = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(3):
            await all_sent.wait()
            while sender.inflight:
                await asyncio.wait(list(sender.inflight))
    finally:
        await sender.aclose()
    assert runner.done() and maintenance == 1
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert len(rows) == 8 and all(row.status == "DONE" for row in rows)


@pytest.mark.parametrize("max_concurrency", [1, 3])
async def test_busy_sender_expires_messages_when_maintenance_interval_elapses(
    pg_rig, max_concurrency
):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = pg_rig
    started = asyncio.Queue()
    parked, release = asyncio.Event(), asyncio.Event()

    class Client:
        async def send(self, request):
            identity = dict(request.headers)["X-Operation-Id"]
            if identity == "0":
                rig.clock.advance(1)
            started.put_nowait(identity)
            await release.wait()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    async def wait(seconds):
        parked.set()
        await asyncio.Event().wait()

    async with rig.factory() as session:
        expired = await rig.outbox.enqueue(session, rig.draft("expire", ttl=timedelta(seconds=1)))
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == expired.message_id)
            .values(next_attempt_at=rig.clock() + timedelta(days=1))
        )
        for number in range(3):
            await rig.outbox.enqueue(session, rig.draft(str(number)))
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {rig.target.name: Client()},
        clock=rig.clock,
        monotonic=rig.clock.monotonic,
        wait=wait,
        budget=ReliableConfig(rig.targets, claim_batch_size=1, max_concurrency=max_concurrency),
    )
    runner = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(3):
            assert await started.get() == "0"
            if max_concurrency == 1:
                await parked.wait()
                release.set()
            assert {await started.get(), await started.get()} == {"1", "2"}
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, expired.message_id)
            assert row.status == "EXPIRED" and row.attempts == 0
    finally:
        release.set()
        await sender.aclose()
    assert runner.done()


async def test_send_completion_cannot_bypass_scan_error_backoff(pg_rig):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    rig = pg_rig
    started = asyncio.Queue()
    release, waiting = asyncio.Event(), asyncio.Event()
    requests = []
    expire_pending = rig.store.expire_pending
    failed = False

    class Client:
        async def send(self, request):
            identity = dict(request.headers)["X-Operation-Id"]
            requests.append((identity, rig.clock.monotonic()))
            started.put_nowait(identity)
            await release.wait()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    async def expire(limit):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("scan unavailable")
        return await expire_pending(limit)

    async def wait(seconds):
        waiting.set()
        await rig.clock.wait(seconds)

    async with rig.factory() as session:
        first = await rig.outbox.enqueue(session, rig.draft("first"))
        second = await rig.outbox.enqueue(session, rig.draft("second"))
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {rig.target.name: Client()},
        clock=rig.clock,
        monotonic=rig.clock.monotonic,
        wait=wait,
        budget=ReliableConfig(rig.targets, max_concurrency=1),
    )
    await sender.tick()
    delivery = next(iter(sender.inflight))
    rig.store.expire_pending = expire
    runner = asyncio.create_task(sender.run())
    try:
        async with asyncio.timeout(3):
            assert await started.get() == "first"
            await waiting.wait()
            release.set()
            await delivery
            async with rig.factory() as session:
                assert (await session.get(ReliableMessage, first.message_id)).status == "DONE"
                assert (await session.get(ReliableMessage, second.message_id)).status == "PENDING"
            rig.clock.advance(1)
            assert await started.get() == "second"
    finally:
        release.set()
        await sender.aclose()
    assert runner.done() and requests == [("first", 0), ("second", 1)]


async def test_loop_survives_scan_error_with_injected_wait(rig, caplog):
    from app.reliable.sender import Sender

    rounds = []
    recovered, waiting = asyncio.Event(), asyncio.Event()

    async def expire(limit):
        rounds.append(limit)
        if len(rounds) == 1:
            raise RuntimeError("sensitive database text")
        recovered.set()
        return 0

    async def wait(seconds):
        waiting.set()
        await rig.clock.wait(seconds)

    rig.store.expire_pending = expire
    sender = Sender(rig.store, {}, {}, {}, wait=wait)
    runner = asyncio.create_task(sender.run())
    async with asyncio.timeout(2):
        await waiting.wait()
        assert not recovered.is_set() and len(rounds) == 1
        rig.clock.advance(1)
        await recovered.wait()
    await sender.aclose()
    assert runner.done() and len(rounds) == 2
    assert "scan failed" in caplog.text
    assert "sensitive database text" not in caplog.text


async def test_shutdown_leaves_unconfirmed_lease_and_closes_other_resources(rig, caplog):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    started, cancelled, unblock, closed = (asyncio.Event() for _ in range(4))

    class Client:
        async def send(self, request):
            started.set()
            try:
                await unblock.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await unblock.wait()
                raise

        async def aclose(self):
            closed.set()

    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {"peer": Client()},
        budget=ReliableConfig(rig.targets, shutdown_grace_s=0.05),
    )
    await sender.tick()
    await started.wait()
    tasks = list(sender.inflight)
    try:
        async with asyncio.timeout(0.5):
            await sender.aclose()
        assert cancelled.is_set() and closed.is_set()
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, result.message_id)
            assert row.status == "SENDING" and row.lease_id is not None
        assert "unconfirmed" in caplog.text
    finally:
        unblock.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_shutdown_uses_one_monotonic_deadline_for_blocked_release_and_close(rig, caplog):
    from app.reliable.config import ReliableConfig
    from app.reliable.sender import Sender

    started, block, disposed = (asyncio.Event() for _ in range(3))
    resources = []

    async def release(claim, targets):
        resources.append(asyncio.current_task())
        rig.clock.advance(0.03)
        await block.wait()

    class Client:
        async def send(self, request):
            started.set()
            await block.wait()

        async def aclose(self):
            resources.append(asyncio.current_task())
            rig.clock.advance(0.03)
            await block.wait()

    async def dispose():
        resources.append(asyncio.current_task())
        disposed.set()

    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    rig.store.release = release
    sender = Sender(
        rig.store,
        rig.targets,
        rig.adapters,
        {"peer": Client()},
        monotonic=rig.clock.monotonic,
        dispose=dispose,
        budget=ReliableConfig(rig.targets, shutdown_grace_s=0.05),
    )
    await sender.tick()
    await started.wait()
    try:
        async with asyncio.timeout(0.5):
            await sender.aclose()
        assert disposed.is_set()
        assert "lease release" in caplog.text and "HTTP close" in caplog.text
        async with rig.factory() as session:
            assert (await session.get(ReliableMessage, result.message_id)).status == "SENDING"
    finally:
        block.set()
        await asyncio.gather(*resources, return_exceptions=True)
