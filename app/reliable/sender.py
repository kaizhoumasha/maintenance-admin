import asyncio
import logging
import random
import time
from datetime import UTC, datetime

from .config import ReliableConfig
from .delivery import send_one

logger = logging.getLogger(__name__)


class Sender:
    def __init__(
        self,
        store,
        targets,
        adapters,
        clients,
        *,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        uniform=random.uniform,
        wait=asyncio.sleep,
        budget=None,
        dispose=None,
    ):
        self.store, self.targets, self.adapters, self.clients = store, targets, adapters, clients
        self.clock, self.monotonic, self.uniform, self.wait = clock, monotonic, uniform, wait
        self.budget = budget or ReliableConfig(targets)
        self.dispose = dispose
        self.inflight = {}
        self.runner = None
        self.stopping = False
        self.closed = False

    async def tick(self) -> int:
        if self.stopping:
            return 0
        limit = self.budget.claim_batch_size
        await self.store.expire_pending(limit)
        await self.store.recover_expired(limit, self.targets)
        free = self.budget.max_concurrency - len(self.inflight)
        if free <= 0 or self.stopping:
            return 0
        served = {
            name: target
            for name, target in self.targets.items()
            if name in self.clients and target.protocol in self.adapters
        }
        claims = await self.store.claim(min(limit, free), served)
        for claim in claims:
            task = asyncio.create_task(
                send_one(
                    claim,
                    store=self.store,
                    targets=self.targets,
                    adapters=self.adapters,
                    clients=self.clients,
                    clock=self.clock,
                    uniform=self.uniform,
                )
            )
            self.inflight[task] = claim
            task.add_done_callback(self._completed)
        return len(claims)

    def _completed(self, task):
        self.inflight.pop(task, None)
        self._observe(task)

    @staticmethod
    def _observe(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("reliable task failed error_type=%s", type(task.exception()).__name__)

    async def run(self) -> None:
        self.runner = asyncio.current_task()
        while not self.stopping:
            try:
                await self.tick()
            except Exception as error:
                logger.error("reliable scan failed error_type=%s", type(error).__name__)
            await self.wait(self.budget.poll_interval_s)

    async def _bounded(self, tasks, deadline, phase, *, cancel=True):
        if not tasks:
            return set()
        done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - self.monotonic()))
        for task in done:
            self._observe(task)
        if pending:
            logger.warning(
                "reliable shutdown unconfirmed phase=%s count=%s",
                phase,
                len(pending),
                extra={"phase": phase, "count": len(pending)},
            )
            for task in pending:
                if cancel:
                    task.cancel()
                task.add_done_callback(self._observe)
        return done

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopping = True
        deadline = self.monotonic() + self.budget.shutdown_grace_s
        if self.runner is not None and not self.runner.done():
            self.runner.cancel()
            await self._bounded(
                {self.runner}, self.monotonic() + max(0, deadline - self.monotonic()) / 8, "scanner"
            )
        inflight = dict(self.inflight)
        await self._bounded(
            set(inflight),
            self.monotonic() + max(0, deadline - self.monotonic()) / 2,
            "inflight",
            cancel=False,
        )
        pending = {task for task in inflight if not task.done()}
        for task in pending:
            task.cancel()
        await self._bounded(
            pending,
            self.monotonic() + max(0, deadline - self.monotonic()) / 3,
            "cancellation",
            cancel=False,
        )
        releases = {
            asyncio.create_task(self.store.release(claim, self.targets))
            for task, claim in inflight.items()
            if task.cancelled()
        }
        await self._bounded(
            releases, self.monotonic() + max(0, deadline - self.monotonic()) / 2, "lease release"
        )
        closures = {asyncio.create_task(client.aclose()) for client in self.clients.values()}
        await self._bounded(
            closures, self.monotonic() + max(0, deadline - self.monotonic()) / 2, "HTTP close"
        )
        if self.dispose is not None:
            await self._bounded({asyncio.create_task(self.dispose())}, deadline, "database dispose")
