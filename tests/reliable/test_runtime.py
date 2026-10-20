import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.controllers.health import router as health_router
from app.core.database import get_session
from app.core.redis import get_redis
from app.http_client import DeliveryState, HttpResult
from app.models import ReliableMessage
from app.reliable.config import ReliableConfig
from app.reliable.sender import Sender


async def test_empty_runtime_keeps_scanning_with_zero_http_clients(pg_rig, pg_url, monkeypatch):
    import app.reliable_runtime as runtime

    rig = pg_rig
    waiting = asyncio.Event()
    parked = asyncio.Event()
    senders, engines = [], []
    create_engine = runtime.create_async_engine

    def engine(*args, **kwargs):
        instance = create_engine(*args, **kwargs)
        engines.append(instance)
        return instance

    async def wait(seconds):
        waiting.set()
        await parked.wait()

    def sender(*args, **kwargs):
        instance = Sender(*args, **kwargs, wait=wait)
        senders.append(instance)
        return instance

    def forbidden(config):
        raise AssertionError("empty targets must not create HTTP clients")

    monkeypatch.setattr(runtime, "Sender", sender)
    monkeypatch.setattr(runtime, "create_async_engine", engine)
    async with runtime.reliable_lifespan(
        ReliableConfig({}), {}, database_url=pg_url, client_factory=forbidden
    ) as outbox:
        assert outbox is None
        async with asyncio.timeout(3):
            await waiting.wait()
        async with rig.factory() as session:
            result = await rig.outbox.enqueue(session, rig.draft())
            await session.commit()
        # 本进程没有配置该目标:不领取,消息保持 PENDING,不被终态失败。
        assert await senders[0].tick() == 0
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, result.message_id)
            assert row.status == "PENDING" and row.attempts == 0
        assert engines[0].pool.size() == 2
        assert engines[0].pool._max_overflow == 0


async def test_two_api_lifespans_have_isolated_budgets_and_management_access(
    pg_rig, pg_url, redis, monkeypatch
):
    import app.reliable_runtime as runtime

    rig = pg_rig
    async with rig.factory() as session:
        for number in range(8):
            await rig.outbox.enqueue(session, rig.draft(str(number)))
        await session.commit()
    ready, unblock = asyncio.Event(), asyncio.Event()
    clients, senders, engines, identities = [], [], [], []
    create_engine = runtime.create_async_engine

    class Client:
        closed = False

        async def send(self, request):
            identities.append(dict(request.headers)["X-Operation-Id"])
            if len(identities) == 8:
                ready.set()
            await unblock.wait()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            self.closed = True

    def client(config):
        instance = Client()
        clients.append(instance)
        return instance

    def engine(*args, **kwargs):
        instance = create_engine(*args, **kwargs)
        engines.append(instance)
        return instance

    async def wait(seconds):
        await unblock.wait()

    def sender(*args, **kwargs):
        instance = Sender(*args, **kwargs, wait=wait)
        senders.append(instance)
        return instance

    @asynccontextmanager
    async def lifespan(application):
        async with runtime.reliable_lifespan(
            ReliableConfig(rig.targets), rig.adapters, database_url=pg_url, client_factory=client
        ) as outbox:
            application.state.outbox = outbox
            yield

    async def sessions():
        async with rig.factory() as session:
            yield session

    monkeypatch.setattr(runtime, "create_async_engine", engine)
    monkeypatch.setattr(runtime, "Sender", sender)
    applications = [FastAPI(lifespan=lifespan) for _ in range(2)]
    for application in applications:
        application.include_router(health_router)
        application.dependency_overrides[get_session] = sessions
        application.dependency_overrides[get_redis] = lambda: redis
    async with AsyncExitStack() as stack:
        for application in applications:
            await stack.enter_async_context(application.router.lifespan_context(application))
        try:
            async with asyncio.timeout(5):
                await ready.wait()
                async with AsyncClient(
                    transport=ASGITransport(app=applications[0]), base_url="http://test"
                ) as api:
                    assert (await api.get("/ready")).status_code == 200
            assert len(set(identities)) == 8 and len(clients) == 2
            assert all(len(instance.inflight) == 4 for instance in senders)
            assert all(
                instance.pool.checkedout() == 0
                and instance.pool.size() == 2
                and instance.pool._max_overflow == 0
                for instance in engines
            )
        finally:
            for instance in senders:
                instance.stopping = True
            unblock.set()
    assert all(instance.closed for instance in clients)
    async with rig.factory() as session:
        rows = await session.scalars(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
        assert all(row.status == "DONE" for row in rows)
