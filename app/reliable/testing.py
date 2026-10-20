import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta

from app.http_client import HttpRequest, HttpResult


class FakeClock:
    def __init__(self, now: datetime | None = None):
        self.current = now or datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0
        self.waiters = []

    def __call__(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds
        for deadline, future in tuple(self.waiters):
            if deadline <= self.elapsed and not future.done():
                future.set_result(None)

    async def wait(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        item = (self.elapsed + seconds, future)
        self.waiters.append(item)
        try:
            await future
        finally:
            self.waiters.remove(item)


class DeterministicRandom:
    def __init__(self, fraction: float = 0.5):
        if not 0 <= fraction <= 1:
            raise ValueError("fraction outside range")
        self.fraction = fraction

    def __call__(self, low: float, high: float) -> float:
        return low + (high - low) * self.fraction


class ScriptedHttpClient:
    def __init__(self, *results: HttpResult):
        self.results = deque(results)
        self.requests: list[HttpRequest] = []
        self.closed = False

    async def send(self, request: HttpRequest) -> HttpResult:
        self.requests.append(request)
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    async def aclose(self) -> None:
        self.closed = True


class FaultInjectingHttpClient:
    def __init__(self, client, *, before=None, after=None):
        self.client = client
        self.before = before
        self.after = after

    async def send(self, request: HttpRequest) -> HttpResult:
        if self.before is not None:
            await self.before(request)
        result = await self.client.send(request)
        if self.after is not None:
            await self.after(request, result)
        return result

    async def aclose(self) -> None:
        await self.client.aclose()
