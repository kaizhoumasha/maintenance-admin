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
        self._wakeup = asyncio.Event()
        self._close_task: asyncio.Task[None] | None = None

    async def tick(self, *, maintain: bool = True) -> int:
        if self.stopping:
            return 0
        limit = self.budget.claim_batch_size
        if maintain:
            await self.store.expire_pending(self.budget.maintenance_batch_size)
            await self.store.recover_expired(self.budget.maintenance_batch_size, self.targets)
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
        self._wakeup.set()

    @staticmethod
    def _observe(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("reliable task failed error_type=%s", type(task.exception()).__name__)

    async def run(self) -> None:
        self.runner = asyncio.current_task()
        maintained_at = None
        timer_fired = True
        while not self.stopping:
            self._wakeup.clear()
            # 过期/租约回收按时间节流:由完成唤醒触发的 tick 只领取,不重复跑维护事务;
            # 轮询计时器到点或出错重试时一定维护。
            now = self.monotonic()
            maintain = (
                timer_fired
                or maintained_at is None
                or now - maintained_at >= self.budget.poll_interval_s
            )
            timer_fired = False
            try:
                claimed = await self.tick(maintain=maintain)
            except Exception as error:
                logger.error("reliable scan failed error_type=%s", type(error).__name__)
                await self.wait(self.budget.poll_interval_s)
                timer_fired = True
                continue
            if maintain:
                maintained_at = now
            if claimed and len(self.inflight) < self.budget.max_concurrency:
                continue
            polling = asyncio.create_task(self.wait(self.budget.poll_interval_s))
            wakeup = asyncio.create_task(self._wakeup.wait())
            try:
                done, _ = await asyncio.wait({polling, wakeup}, return_when=asyncio.FIRST_COMPLETED)
                timer_fired = polling in done
            finally:
                polling.cancel()
                wakeup.cancel()
                await asyncio.gather(polling, wakeup, return_exceptions=True)

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
        if self._close_task is None:
            self.closed = True
            self.stopping = True
            deadline = self.monotonic() + self.budget.shutdown_grace_s
            self._close_task = asyncio.create_task(self._close(deadline))
            self._close_task.add_done_callback(self._observe)
        await asyncio.shield(self._close_task)

    async def _close(self, deadline: float) -> None:
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
