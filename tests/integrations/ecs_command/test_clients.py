import pytest

from app.http_client import HttpClientConfig


async def test_cache_shares_origin_and_respects_inflight_capacity():
    from app.integrations.ecs_command.clients import DynamicClients, PoolUnavailable

    made = []

    class Client:
        closed = False

        async def aclose(self):
            self.closed = True

    def factory(config):
        client = Client()
        made.append(client)
        return client

    pool = DynamicClients(factory=factory, capacity=1)
    async with pool.borrow(HttpClientConfig("http://a.test", "a")) as first:
        async with pool.borrow(HttpClientConfig("http://a.test", "b")) as second:
            assert first is second
        with pytest.raises(PoolUnavailable):
            async with pool.borrow(HttpClientConfig("http://b.test", "b")):
                pass
    async with pool.borrow(HttpClientConfig("http://b.test", "b")):
        assert made[0].closed
    await pool.aclose()
    assert all(c.closed for c in made)


async def test_concurrent_borrow_creates_one_client_and_cancel_releases_slot():
    import asyncio

    from app.integrations.ecs_command.clients import DynamicClients

    made = []

    class Client:
        async def aclose(self):
            pass

    def factory(config):
        made.append(config.base_url)
        return Client()

    pool = DynamicClients(factory=factory, capacity=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def borrow():
        async with pool.borrow(HttpClientConfig("http://a.test", "a")):
            entered.set()
            await release.wait()

    tasks = [asyncio.create_task(borrow()) for _ in range(8)]
    await entered.wait()
    await asyncio.sleep(0)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert made == ["http://a.test"]
    async with pool.borrow(HttpClientConfig("http://b.test", "b")):
        pass
    await pool.aclose()


async def test_failed_close_does_not_free_capacity_for_replacement():
    from app.integrations.ecs_command.clients import DynamicClients, PoolUnavailable

    made = []

    class Client:
        attempts = 0

        async def aclose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("close failed")

    def factory(config):
        made.append(Client())
        return made[-1]

    pool = DynamicClients(factory=factory, capacity=1)
    async with pool.borrow(HttpClientConfig("http://a.test", "a")):
        pass
    with pytest.raises(RuntimeError):
        async with pool.borrow(HttpClientConfig("http://b.test", "b")):
            pass
    with pytest.raises(PoolUnavailable):
        async with pool.borrow(HttpClientConfig("http://b.test", "b")):
            pass
    assert len(made) == 1
    await pool.aclose()


async def test_default_capacity_counts_all_borrowed_clients():
    from contextlib import AsyncExitStack

    from app.integrations.ecs_command.clients import DynamicClients, PoolUnavailable

    class Client:
        async def aclose(self):
            pass

    pool = DynamicClients(factory=lambda _: Client())
    async with AsyncExitStack() as stack:
        for i in range(64):
            await stack.enter_async_context(
                pool.borrow(HttpClientConfig(f"http://ecs-{i}.test", "ecs"))
            )
        with pytest.raises(PoolUnavailable):
            async with pool.borrow(HttpClientConfig("http://overflow.test", "ecs")):
                pass
    await pool.aclose()
