import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import replace

from app.http_client import AsyncHttpClient
from app.reliable.client_provider import PoolUnavailable


class DynamicClients:
    def __init__(self, *, factory=AsyncHttpClient, capacity=64):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("invalid client capacity")
        self.factory, self.capacity = factory, capacity
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()
        self.closed = False
        self.released = asyncio.Event()

    @asynccontextmanager
    async def borrow(self, config):
        key = replace(config, destination_key="snapshot")
        try:
            async with asyncio.timeout(config.pool_timeout_s), self.lock:
                if self.closed:
                    raise PoolUnavailable("client provider closed")
                entry = self.entries.get(key)
                if entry is None:
                    if len(self.entries) >= self.capacity:
                        candidate = next(
                            ((k, v) for k, v in self.entries.items() if v[1] == 0 and not v[2]),
                            None,
                        )
                        if candidate is None:
                            raise PoolUnavailable("all clients in use or closing")
                        old, item = candidate
                        item[2] = True
                        await item[0].aclose()
                        del self.entries[old]
                    entry = [self.factory(config), 0, False]
                    self.entries[key] = entry
                if entry[2]:
                    raise PoolUnavailable("client is closing")
                entry[1] += 1
                self.entries.move_to_end(key)
        except TimeoutError:
            raise PoolUnavailable("client acquisition timed out") from None
        try:
            yield entry[0]
        finally:
            # No await between decrement and release: cancellation cannot leak a borrower.
            entry[1] -= 1
            self.released.set()

    async def aclose(self):
        self.closed = True
        async with asyncio.timeout(15):
            async with self.lock:
                while any(entry[1] for entry in self.entries.values()):
                    self.released.clear()
                    await self.released.wait()
                for entry in self.entries.values():
                    await entry[0].aclose()
                self.entries.clear()
