"""Independent AUTO transactions with bounded whole-transaction retries."""

import logging
from time import perf_counter

from sqlalchemy.exc import DBAPIError

from app.businesses._components.bin_roller.service import RackLockSetChanged
from app.businesses.runtime import Capabilities
from app.reliable.lifecycle import EvidencePersistenceError, database_failure_code

from .task_service import AutoTaskService

logger = logging.getLogger(__name__)


class AutoS1Transactions:
    def __init__(
        self, capabilities: Capabilities, *, task_flow=None, first_plan_wait_ms=None, service=None
    ):
        self.capabilities = capabilities
        self.host = (
            service
            if service is not None
            else AutoTaskService(wms=capabilities.wms, first_plan_wait_ms=first_plan_wait_ms)
        )
        self.task_flow = task_flow
        self.service = service

    async def advance(
        self, kind: str, record_id: str | int, *, now_ms: int, operation=None, timing=None
    ):
        if self.service is None and kind not in {
            "PREPARE_NEXT",
            "PREPARE_RESULT",
            "TASK",
            "BIN_RELEASE",
        }:
            raise ValueError("unsupported AUTO S1 work kind")
        if self.service is None and kind in {"TASK", "BIN_RELEASE"} and self.task_flow is None:
            raise ValueError("AUTO S5 host assembly required")
        retry_elapsed_ms = 0.0
        for attempt in range(3):
            metrics = (
                {**timing, "workline_lock_ms": 0.0, "all_locks_ms": 0.0}
                if timing is not None
                else None
            )
            started = perf_counter()
            commit_started = None
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={
                                "isolation_level": "REPEATABLE READ"
                                if kind
                                in {
                                    "INBOUND_REPORT",
                                    "OUTBOUND_REPORT",
                                    "SCAN2_RELEASE",
                                    "BIN_RELEASE_RESULT",
                                    "ARCHIVE",
                                }
                                else "SERIALIZABLE"
                            }
                        )
                    if self.service is not None:
                        if metrics is None:
                            return await self.service.advance_original(
                                session, kind, record_id, now_ms=now_ms
                            )
                        outcome = await self.service.advance_original(
                            session,
                            kind,
                            record_id,
                            now_ms=now_ms,
                            operation=operation,
                            timing=metrics,
                        )
                        commit_started = perf_counter()
                    elif kind == "TASK":
                        return await self.task_flow.advance(
                            session, task_id=record_id, now_ms=now_ms
                        )
                    elif kind == "BIN_RELEASE":
                        work_line_id, arrival_id = record_id
                        return await self.task_flow.release_bin(
                            session, work_line_id=work_line_id, arrival_id=arrival_id, now_ms=now_ms
                        )
                    elif kind == "PREPARE_NEXT":
                        return await self.host.prepare_next(
                            session, work_line_id=record_id, now_ms=now_ms
                        )
                    else:
                        return await self.host.advance_prepare(
                            session, task_id=record_id, now_ms=now_ms
                        )
            except RackLockSetChanged:
                if attempt == 2:
                    raise
                retry = "LOCK_SET_CHANGED"
            except (DBAPIError, EvidencePersistenceError) as error:
                if database_failure_code(error) != "40001" or attempt == 2:
                    raise
                retry = "40001"
            else:
                if metrics is not None:
                    finished = perf_counter()
                    locks = metrics.pop("all_locks_ms")
                    metrics.update(
                        attempt=attempt + 1,
                        related_locks_ms=max(0, locks - metrics["workline_lock_ms"]),
                        prepare_ms=max(
                            0,
                            (commit_started - started) * 1000
                            - locks
                            - metrics.get("processing_ms", 0),
                        ),
                        commit_ms=(finished - commit_started) * 1000,
                        retry_elapsed_ms=retry_elapsed_ms,
                        total_ms=retry_elapsed_ms + (finished - started) * 1000,
                    )
                    logger.info("SMT AUTO outbound advance timing %s", metrics)
                return outcome
            if metrics is not None:
                elapsed_ms = (perf_counter() - started) * 1000
                retry_elapsed_ms += elapsed_ms
                logger.info(
                    "SMT AUTO outbound advance retry %s",
                    {
                        **metrics,
                        "attempt": attempt + 1,
                        "reason": retry,
                        "elapsed_ms": elapsed_ms,
                    },
                )
