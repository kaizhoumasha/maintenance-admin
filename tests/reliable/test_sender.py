import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.http_client import DeliveryState, HttpResult
from app.models import ReliableMessage


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


async def test_loop_survives_scan_error_with_injected_wait(rig, caplog):
    from app.reliable.sender import Sender

    rounds = []
    recovered = asyncio.Event()

    async def expire(limit):
        rounds.append(limit)
        if len(rounds) == 1:
            raise RuntimeError("sensitive database text")
        recovered.set()
        return 0

    async def wait(seconds):
        if len(rounds) == 1:
            return
        await asyncio.Event().wait()

    rig.store.expire_pending = expire
    sender = Sender(rig.store, {}, {}, {}, wait=wait)
    runner = asyncio.create_task(sender.run())
    async with asyncio.timeout(2):
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
