"""One host, original business records, and one whole-transaction pending entry."""

import logging

from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.businesses._components.bin_roller.service import (
    ROLLER_SOURCES,
    BinRoller,
    RackLockSetChanged,
)
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses._components.sorting_task.service import SortingTask
from app.businesses.runtime import Capabilities
from app.core.clock import unix_ms
from app.diagnostics import log_exception
from app.integrations.ecs_command import CommandOwner

from .manual_bin_flow import ManualBinFlow
from .manual_rack_flow import MANUAL_SOURCES
from .models import RACK_COMPLETION_OPERATIONS
from .repository import TaskRepository
from .roller_ports import RollerPorts
from .task_flow import SmtManualTaskFlow

logger = logging.getLogger(__name__)
ROLLER_KINDS = ("PASSAGE", "SCAN2_RELEASE", "RETURN", "FACE", "RACK_CYCLE", "DRAIN")
KINDS = ("TASK", "MANUAL_BIN", "MANUAL_COMPLETION", "MANUAL_RACK", "RACK_REPORT", *ROLLER_KINDS)


class SmtManualService(SmtManualTaskFlow):
    def __init__(self, capabilities: Capabilities, *, first_plan_wait_ms: int | None = None):
        repository = TaskRepository()
        self.ports = RollerPorts(repository)
        self.rack_dispatch = RackDispatch("smt_manual", capabilities, task_admission=self.ports)
        roller = BinRoller(
            CommandOwner("smt_manual"), capabilities, self.ports, rack_dispatch=self.rack_dispatch
        )
        super().__init__(
            capabilities, roller, repository=repository, first_plan_wait_ms=first_plan_wait_ms
        )
        self.manual_bins = ManualBinFlow(capabilities, roller, repository=repository)
        self.ports.manual_bins = self.manual_bins
        self.components = {
            "sorting_task": SortingTask("smt_manual", self),
            "bin_roller": roller,
            "rack_dispatch": self.rack_dispatch,
        }

    async def read_current_task(self, session, *, work_line_id):
        return await self.ports.read_current_task(session, work_line_id=work_line_id)

    async def read_successor(self, session, *, work_line_id):
        return await self.ports.read_successor(session, work_line_id=work_line_id)

    async def accept_scan2(self, ctx, arrival):
        return await self.manual_bins.accept_scan2(ctx, arrival)

    async def accept_manual_completed(self, ctx, payload):
        return await self.manual_bins.accept_manual_completed(ctx, payload)

    async def accept_direct_pick_completed(self, ctx, payload):
        return await self.manual_racks.accept_direct_pick_completed(ctx, payload)

    async def accept_return_rack_completed(self, ctx, payload):
        return await self.manual_racks.accept_return_rack_completed(ctx, payload)

    def _pending_tasks(self):
        task, notice, confirmation = (
            self.repository.tables[key] for key in ("task", "input", "request")
        )
        return select(task.c.task_id).where(
            or_(
                task.c.status.in_(("QUEUED", "PREPARING", "EXECUTING")),
                select(notice.c.operation_id)
                .where(
                    notice.c.task_id == task.c.task_id,
                    notice.c.operation.in_(RACK_COMPLETION_OPERATIONS),
                    notice.c.state == "PENDING",
                )
                .exists(),
            )
        )

    def _pending_owner(self, kind):
        if kind in ROLLER_KINDS:
            return self.roller
        return self.manual_racks if kind in {"MANUAL_RACK", "RACK_REPORT"} else self.manual_bins

    async def scan_pending(self, *, limit: int = 100) -> int:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        task_id = self.repository.tables["task"].c.task_id
        async with self.capabilities.session_factory() as snapshot, snapshot.begin():
            if snapshot.get_bind().dialect.name == "postgresql":
                # Peer task/operation IDs need not increase with local acceptance time.
                await snapshot.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await snapshot.execute(text("SET TRANSACTION READ ONLY"))
            upper = {
                "TASK": await snapshot.scalar(
                    self._pending_tasks().order_by(task_id.desc()).limit(1)
                )
            }
            for kind in KINDS[1:]:
                upper[kind] = await self._pending_owner(kind).get_pending_upper_id(
                    snapshot, kind=kind
                )
            cursors = {kind: None for kind in KINDS if upper[kind] is not None}
            total = 0
            while cursors:
                for kind in tuple(cursors):
                    if kind == "TASK":
                        query = self._pending_tasks().where(task_id <= upper[kind])
                        if cursors[kind] is not None:
                            query = query.where(task_id > cursors[kind])
                        ids = tuple(await snapshot.scalars(query.order_by(task_id).limit(limit)))
                    else:
                        page = await self._pending_owner(kind).list_pending(
                            snapshot,
                            kind=kind,
                            after_id=cursors[kind],
                            upper_id=upper[kind],
                            limit=limit,
                        )
                        ids = tuple(w.record_id for w in page) if kind in ROLLER_KINDS else page
                    if not ids:
                        del cursors[kind]
                        continue
                    for record_id in ids:
                        try:
                            await self.advance(kind, record_id)
                        except Exception as error:
                            log_exception(
                                logger,
                                "SMT manual advance failed",
                                error,
                                component="smt_manual",
                                phase="advance",
                                context={"request_id": record_id},
                            )
                        total += 1
                    cursors[kind] = ids[-1]
            return total

    async def advance(self, kind: str, record_id: str) -> str:
        if kind not in KINDS:
            raise ValueError("unsupported manual work kind")
        for attempt in range(3):
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={
                                "isolation_level": "SERIALIZABLE"
                                if kind
                                in {
                                    "TASK",
                                    "PASSAGE",
                                    "FACE",
                                    "RETURN",
                                    "RACK_CYCLE",
                                    "DRAIN",
                                    "MANUAL_RACK",
                                    "MANUAL_BIN",
                                    "MANUAL_COMPLETION",
                                }
                                else "REPEATABLE READ"
                            }
                        )
                    return await self._advance(session, kind, record_id)
            except RackLockSetChanged:
                if attempt == 2:
                    raise
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def _advance(self, session, kind, record_id):
        if kind not in {"SCAN2_RELEASE", "RACK_REPORT"}:
            if kind == "TASK":
                located = await self.repository.task(session, record_id)
            elif kind in {"RACK_CYCLE", "MANUAL_RACK"}:
                job = await self.rack_dispatch.progress(session, job_id=record_id)
                kinds = MANUAL_SOURCES if kind == "MANUAL_RACK" else ROLLER_SOURCES
                located = (
                    {"work_line_id": job.origin.work_line_id, "task_id": job.origin.task_id}
                    if job and job.origin.source_kind in kinds
                    else None
                )
            elif kind in {"MANUAL_BIN", "MANUAL_COMPLETION"}:
                key = "manual_bin" if kind == "MANUAL_BIN" else "input"
                table = self.repository.tables[key]
                column = table.c.arrival_id if kind == "MANUAL_BIN" else table.c.operation_id
                located = await self.manual_bins._one(session, key, column == record_id)
            else:
                key = {
                    "PASSAGE": "bin_passage",
                    "RETURN": "bin_return",
                    "FACE": "face_work",
                    "DRAIN": "workline_state",
                }.get(kind, kind.lower())
                located = await self.roller.original_record(session, key, record_id)
            if not located:
                return "NOT_FOUND"
            task_id = located.get("task_id")
            if kind == "FACE" and located["purpose"] == "SUPPLY":
                task_id = located["scope_id"]
            elif kind == "PASSAGE" and located["scope_kind"] == "TASK":
                task_id = located["scope_id"]
            await self.repository.lock_dispatch_line(
                session, self.roller, located["work_line_id"], task_id=task_id
            )
        if kind not in {"SCAN2_RELEASE", "RACK_REPORT"} and task_id:
            await self._register_task_rack_candidates(session, task_id=task_id)
        if kind == "TASK":
            original = await self.repository.task(session, record_id)
            if original is None:
                return "NOT_FOUND"
            if original["status"] == "QUEUED":
                await self.prepare_next(
                    session,
                    work_line_id=original["work_line_id"],
                    locked=True,
                    now_ms=unix_ms(self.capabilities.wms.clock()),
                )
            await self.manual_racks.reconcile_task(session, task_id=record_id)
            return await self.advance_task(session, task_id=record_id, locked=True)
        if kind == "RACK_REPORT":
            return await self.manual_racks.advance_report(session, operation_id=record_id)
        if kind == "MANUAL_BIN":
            return await self.manual_bins.advance_bin(
                session,
                arrival_id=record_id,
                locked=True,
                now_ms=unix_ms(self.capabilities.wms.clock()),
            )
        if kind == "MANUAL_COMPLETION":
            return await self.manual_bins.advance_completion(
                session, operation_id=record_id, locked=True
            )
        if kind == "MANUAL_RACK":
            await self.manual_racks.reconcile_task(session, task_id=task_id)
            return await self.manual_racks.advance_rack(session, rack_work_id=record_id)
        if kind == "SCAN2_RELEASE":
            located = await self.roller.original_record(session, "bin_passage", record_id)
            await self.repository.lock_line(session, located["work_line_id"])
            await self.roller.original_record(session, "bin_passage", record_id, for_update=True)
        return await self.roller.advance(session, RollerWorkRef(kind, record_id))
