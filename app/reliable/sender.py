import asyncio
import logging
import random
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from app.diagnostics import diagnose, diagnostic_text, error_cause, log_exception

from .config import ReliableConfig
from .delivery import send_one

logger = logging.getLogger(__name__)


def _log_failure(message, error, *, diagnostic, phase="delivery", retry_in_s=None):
    log_exception(
        logger,
        message,
        error,
        component="reliable",
        phase=phase,
        context={"task": "reliable_sender", "retry_in_s": retry_in_s},
        diagnostic=diagnostic,
    )


@dataclass
class _ScanFailure:
    key: tuple
    phase: str
    started_at: float
    reported_at: float
    diagnostic: dict[str, str]
    count: int = 1
    suppressed: int = 0

    @property
    def log_fields(self) -> dict[str, str | int]:
        return {
            **self.diagnostic,
            "component": "reliable",
            "task": "reliable_sender",
            "phase": self.phase,
            "failure_count": self.count,
            "suppressed_count": self.suppressed,
        }


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
        provider=None,
    ):
        self.store, self.targets, self.adapters, self.clients = store, targets, adapters, clients
        self.clock, self.monotonic, self.uniform, self.wait = clock, monotonic, uniform, wait
        self.budget = budget or ReliableConfig(targets)
        self.dispose = dispose
        self.provider = provider
        self.inflight = {}
        self.runner = None
        self.stopping = False
        self.closed = False
        self._wakeup = asyncio.Event()
        self._close_task: asyncio.Task[None] | None = None
        self._scan_phase = "scan"
        self._scan_failure: _ScanFailure | None = None

    async def tick(self, *, maintain: bool = True) -> int:
        if self.stopping:
            return 0
        self._scan_phase = "scan"
        limit = self.budget.claim_batch_size
        if maintain:
            if hasattr(self.store, "maintain_snapshots"):
                self._scan_phase = "maintain_snapshots"
                await self.store.maintain_snapshots(
                    self.budget.maintenance_batch_size,
                    self.targets,
                    time.monotonic() + self.budget.poll_interval_s,
                )
            self._scan_phase = "expire_pending"
            await self.store.expire_pending(self.budget.maintenance_batch_size)
            self._scan_phase = "recover_expired"
            await self.store.recover_expired(self.budget.maintenance_batch_size, self.targets)
        free = self.budget.max_concurrency - len(self.inflight)
        if free <= 0 or self.stopping:
            return 0
        self._scan_phase = "claim"
        served = {
            name: target
            for name, target in self.targets.items()
            if target.protocol in self.adapters
            and (
                (target.address_mode == "STATIC" and name in self.clients)
                or (
                    target.address_mode == "SNAPSHOT"
                    and self.provider is not None
                    and target.protocol in self.store.snapshots.lifecycles
                )
            )
        }
        claims = await self.store.claim(min(limit, free), served)
        self._scan_phase = "dispatch"
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
                    provider=self.provider,
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
    def _observe(task, *, phase="delivery"):
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            _log_failure("reliable task failed", error, diagnostic=diagnose(error), phase=phase)

    def _record_scan_failure(self, error: Exception) -> None:
        now = self.monotonic()
        cause = error_cause(error)
        frames = list(traceback.walk_tb(error.__traceback__))
        origin = (frames[-1][0].f_code, frames[-1][1]) if frames else None
        diagnostic = diagnose(error)
        key = (
            self._scan_phase,
            type(error),
            type(cause),
            getattr(cause, "sqlstate", None),
            origin,
            diagnostic.get("missing"),
            diagnostic.get("constraint"),
            diagnostic.get("origin"),
        )
        failure = self._scan_failure
        if failure is not None and failure.key == key:
            failure.count += 1
            failure.suppressed += 1
            if now - failure.reported_at >= 60:
                self._summarize_scan_failure(now, "continuing")
            return
        self._summarize_scan_failure(now, "changed")
        self._scan_failure = _ScanFailure(key, self._scan_phase, now, now, diagnostic)
        _log_failure(
            "reliable scan failed",
            error,
            diagnostic=diagnostic,
            phase=self._scan_phase,
            retry_in_s=self.budget.poll_interval_s,
        )

    def _summarize_scan_failure(self, now: float, reason: str) -> None:
        failure = self._scan_failure
        if failure is None or not failure.suppressed:
            return
        logger.warning(
            "reliable scan failure summary task=reliable_sender phase=%s %s "
            "failure_count=%s suppressed_count=%s elapsed_s=%.1f reason=%s",
            failure.phase,
            diagnostic_text(failure.diagnostic),
            failure.count,
            failure.suppressed,
            now - failure.started_at,
            reason,
            extra=failure.log_fields,
        )
        failure.suppressed = 0
        failure.reported_at = now

    def _record_scan_recovery(self) -> None:
        failure = self._scan_failure
        # 满并发等提前返回没有重新验证领取/派发,不能宣称整轮扫描恢复。
        if failure is None or self._scan_phase != "dispatch":
            return
        logger.info(
            "reliable scan recovered task=reliable_sender phase=%s "
            "failure_count=%s suppressed_count=%s elapsed_s=%.1f scope=scan_only",
            failure.phase,
            failure.count,
            failure.suppressed,
            self.monotonic() - failure.started_at,
            extra=failure.log_fields,
        )
        self._scan_failure = None

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
                self._record_scan_failure(error)
                await self.wait(self.budget.poll_interval_s)
                timer_fired = True
                continue
            self._record_scan_recovery()
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
            self._observe(task, phase=phase)
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
                task.add_done_callback(partial(self._observe, phase=phase))
        return done

    async def aclose(self) -> None:
        if self._close_task is None:
            self.closed = True
            self.stopping = True
            deadline = self.monotonic() + self.budget.shutdown_grace_s
            self._close_task = asyncio.create_task(self._close(deadline))
            self._close_task.add_done_callback(partial(self._observe, phase="shutdown"))
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
        if self.provider is not None:
            closures.add(asyncio.create_task(self.provider.aclose()))
        await self._bounded(
            closures, self.monotonic() + max(0, deadline - self.monotonic()) / 2, "HTTP close"
        )
        if self.dispose is not None:
            await self._bounded({asyncio.create_task(self.dispose())}, deadline, "database dispose")
