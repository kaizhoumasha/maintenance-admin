from typing import Protocol


class PoolUnavailable(Exception):
    """No client was borrowed; no HTTP request has started."""


class ClientProvider(Protocol):
    def borrow(self, config): ...
    async def aclose(self): ...
