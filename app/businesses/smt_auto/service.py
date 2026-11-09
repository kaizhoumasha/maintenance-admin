"""AUTO runtime and business-owned pending advancement."""

import logging

from sqlalchemy import or_, select, text

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.businesses._components.bin_roller.service import ROLLER_SOURCES, BinRoller
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses._components.sorting_task.service import SortingTask
from app.core.clock import unix_ms
from app.diagnostics import log_exception
from app.integrations.ecs_command import CommandOwner
from app.models import WorkLine

from .archive import RETENTION_MS, Archive
from .locking import lock_line
from .models import (
    AutoBinWork,
    AutoInboundPlate,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoTask,
)
from .queue import QueueKey
from .rack_ports import AutoRackPorts
from .roller_ports import RollerPorts
from .task_flow import TaskFlow
from .task_service import AutoTaskService
from .transactions import AutoS1Transactions

logger = logging.getLogger(__name__)
RACK_SOURCES = ("AUTO_INBOUND_SOURCE", "AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET")
ROLLER_RECORDS = {
    "PASSAGE": "bin_passage",
    "SCAN2_RELEASE": "bin_passage",
    "RETURN": "bin_return",
    "FACE": "face_work",
    "DRAIN": "workline_state",
}
ROLLER_KINDS = (*ROLLER_RECORDS, "RACK_CYCLE")
PLATE_MODELS = {
    "INBOUND_PLATE": AutoInboundPlate,
    "INBOUND_REPORT": AutoInboundPlate,
    "OUTBOUND_PLATE": AutoOutboundPlate,
    "OUTBOUND_REPORT": AutoOutboundPlate,
}


class AutoTaskQueue:
    """Encapsulates queue eligibility and stable ordering for AUTO tasks."""

    def eligible(self, tasks, *, now_ms: int):
        return sorted(
            (
                task
                for task in tasks
                if getattr(task, "status", None) in (None, "QUEUED")
                and (task.not_before_ms is None or task.not_before_ms <= now_ms)
            ),
            key=lambda task: QueueKey(task.dispatch_sequence, task.issued_timestamp, task.task_id),
        )


class SmtAutoService(AutoTaskService):
    def __init__(self, capabilities, *, worklines, first_plan_wait_ms=None):
        from .inbound import Inbound, PhysicalSettings
        from .outbound import Outbound
        from .outbound_racks import OutboundRacks
        from .source_flow import SourceFlow

        self.capabilities = capabilities
        self.ports = RollerPorts()
        self.dispatch = RackDispatch("smt_auto", capabilities, task_admission=self)
        roller = BinRoller(
            CommandOwner("smt_auto"), capabilities, self.ports, rack_dispatch=self.dispatch
        )
        super().__init__(wms=capabilities.wms, roller=roller, first_plan_wait_ms=first_plan_wait_ms)
        physical = tuple(
            PhysicalSettings(
                line.work_line_id,
                line.work_line_version,
                line.rack_arm_timeout_ms,
                line.bin_arm_timeout_ms,
                line.source_locations,
            )
            for line in worklines
        )
        args = (capabilities.wms, capabilities.ecs, self.dispatch, roller)
        self.inbound = Inbound(*args, settings=physical)
        self.outbound = Outbound(*args, settings=physical)
        self.racks = AutoRackPorts(self.dispatch, inbound=self.inbound, outbound=self.outbound)
        self.work_plans = self.inbound.work_plans
        self.work_plans.roller = roller
        self.task_flow = TaskFlow(
            self.wms,
            roller=roller,
            dispatch=self.dispatch,
            work_plans=self.work_plans,
            inbound=self.inbound,
            outbound=self.outbound,
        )
        self.ports.task_flow = self.task_flow
        self.archive = Archive(self.task_flow, self.racks)
        self.transactions = AutoS1Transactions(capabilities, service=self)
        self.components = {
            "sorting_task": SortingTask("smt_auto", self, task_table=AutoTask.__table__),
            "bin_roller": roller,
            "rack_dispatch": self.dispatch,
        }
        self.rack_flows = {}
        for line in worklines:
            self.rack_flows[line.work_line_id] = (
                SourceFlow(self.inbound, station_id=line.transport_station_id),
                OutboundRacks(self.outbound, station_id=line.transport_station_id),
            )

    async def allows_task_entry(self, session, *, work_line_id, task_id):
        current = await self.ports.read_current_task(session, work_line_id=work_line_id)
        return (
            current.is_valid
            and current.task_id == task_id
            and await self._configured(session, work_line_id)
        )

    async def _configured(self, session, work_line_id):
        settings = self.inbound.settings.get(work_line_id)
        line = await session.get(WorkLine, work_line_id, populate_existing=True)
        return bool(
            settings
            and line
            and line.is_enabled
            and line.business_code == "smt_auto"
            and line.version == settings.work_line_version
        )

    async def advance(self, kind, record_id, *, now_ms=None):
        if now_ms is None:
            now_ms = unix_ms(self.wms.clock())
        return await self.transactions.advance(kind, record_id, now_ms=now_ms)

    def _pending(self, now_ms):
        return {
            "TASK": select(AutoTask.task_id).where(
                or_(
                    AutoTask.status.in_(("QUEUED", "PREPARING", "EXECUTING")),
                    (AutoTask.status == "CANCELLED") & AutoTask.pending,
                )
            ),
            "BIN": select(AutoBinWork.id).where(AutoBinWork.pending),
            "BIN_RELEASE_RESULT": select(AutoBinWork.id).where(
                AutoBinWork.release_command_code.is_not(None),
                AutoBinWork.release_consumed_at_ms.is_(None),
            ),
            "OUTBOUND_FACE": select(AutoOutboundSourceMember.id).where(
                AutoOutboundSourceMember.arrived_at_ms.is_not(None),
                AutoOutboundSourceMember.terminal_basis.is_(None),
            ),
            "INBOUND_PLATE": select(AutoInboundPlate.id).where(AutoInboundPlate.pending),
            "OUTBOUND_PLATE": select(AutoOutboundPlate.id).where(AutoOutboundPlate.pending),
            "INBOUND_REPORT": select(AutoInboundPlate.id).where(
                AutoInboundPlate.report_message_id.is_not(None),
                AutoInboundPlate.report_consumed_at_ms.is_(None),
            ),
            "OUTBOUND_REPORT": select(AutoOutboundPlate.id).where(
                AutoOutboundPlate.report_message_id.is_not(None),
                AutoOutboundPlate.report_consumed_at_ms.is_(None),
            ),
            "ARCHIVE": select(AutoTask.task_id).where(
                AutoTask.business_type == "inbound",
                AutoTask.status.in_(("COMPLETED", "CANCELLED")),
                ~AutoTask.details_cleaned,
                AutoTask.closed_at_ms <= now_ms - RETENTION_MS,
            ),
        }

    async def scan_pending(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid pending page size")
        now_ms = unix_ms(self.wms.clock())
        queries = self._pending(now_ms)
        async with self.capabilities.session_factory() as snapshot, snapshot.begin():
            if snapshot.get_bind().dialect.name == "postgresql":
                await snapshot.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                await snapshot.execute(text("SET TRANSACTION READ ONLY"))
            upper = {}
            for kind, query in queries.items():
                column = query.selected_columns[0]
                upper[kind] = await snapshot.scalar(query.order_by(column.desc()).limit(1))
            upper["RACK"] = await self.dispatch.pending_upper_id(
                snapshot, source_kinds=RACK_SOURCES
            )
            for kind in ROLLER_KINDS:
                upper[kind] = await self.roller.get_pending_upper_id(snapshot, kind=kind)
            cursors = {kind: None for kind, last in upper.items() if last is not None}
            total = 0
            while cursors:
                for kind, after in tuple(cursors.items()):
                    if kind == "RACK":
                        ids = await self.dispatch.pending_ids(
                            snapshot,
                            source_kinds=RACK_SOURCES,
                            after_id=after,
                            upper_id=upper[kind],
                            limit=limit,
                        )
                    elif kind in ROLLER_KINDS:
                        page = await self.roller.list_pending(
                            snapshot, kind=kind, after_id=after, upper_id=upper[kind], limit=limit
                        )
                        ids = tuple(record.record_id for record in page)
                    else:
                        query = queries[kind]
                        column = query.selected_columns[0]
                        query = query.where(column <= upper[kind])
                        if after is not None:
                            query = query.where(column > after)
                        ids = tuple(await snapshot.scalars(query.order_by(column).limit(limit)))
                    if not ids:
                        del cursors[kind]
                        continue
                    for record_id in ids:
                        try:
                            await self.advance(kind, record_id, now_ms=now_ms)
                        except Exception as error:
                            log_exception(
                                logger,
                                "SMT AUTO advance failed",
                                error,
                                component="smt_auto",
                                phase="advance",
                                context={"request_id": str(record_id)},
                            )
                        total += 1
                    cursors[kind] = ids[-1]
            return total

    async def advance_original(self, session, kind, record_id, *, now_ms):
        if kind in ("INBOUND_REPORT", "OUTBOUND_REPORT"):
            flow = self.inbound if kind == "INBOUND_REPORT" else self.outbound
            return await flow.advance_report(session, plate_id=record_id, now_ms=now_ms)
        if kind == "ARCHIVE":
            return await self.archive.clean(session, task_id=record_id, now_ms=now_ms)
        if kind == "BIN_RELEASE_RESULT":
            work = await session.get(AutoBinWork, record_id)
            if work is None:
                return "NOT_FOUND"
            return await self.task_flow.consume_release(
                session, work_line_id=work.work_line_id, arrival_id=work.arrival_id, now_ms=now_ms
            )
        if kind in ("RACK", "RACK_CYCLE"):
            job = await self.dispatch.progress(session, job_id=record_id)
            allowed = RACK_SOURCES if kind == "RACK" else ROLLER_SOURCES
            if job is None or job.origin.source_kind not in allowed:
                return "NOT_FOUND"
            line_id = job.origin.work_line_id
            await lock_line(
                session, self.dispatch, line_id, include_job_ids=(record_id,), roller=self.roller
            )
            if kind == "RACK":
                return await self.racks.advance(
                    session,
                    job_id=record_id,
                    now_ms=now_ms,
                    allow_new=await self._configured(session, line_id),
                )
            return await self.roller.advance(session, RollerWorkRef(kind, record_id))
        if kind in ROLLER_RECORDS:
            original = await self.roller.original_record(session, ROLLER_RECORDS[kind], record_id)
            line_id = original["work_line_id"]
            if kind == "SCAN2_RELEASE":
                await session.execute(
                    select(WorkLine.id).where(WorkLine.id == line_id).with_for_update()
                )
                state = await self.task_flow.consume_release(
                    session, work_line_id=line_id, arrival_id=record_id, now_ms=now_ms
                )
                await self.roller.original_record(
                    session, "bin_passage", record_id, for_update=True
                )
                await self.roller.advance(session, RollerWorkRef(kind, record_id))
                return state
            await lock_line(session, self.dispatch, line_id, roller=self.roller)
            return await self.roller.advance(session, RollerWorkRef(kind, record_id))

        model = {
            "TASK": AutoTask,
            "BIN": AutoBinWork,
            "OUTBOUND_FACE": AutoOutboundSourceMember,
            **PLATE_MODELS,
        }.get(kind)
        if model is None:
            raise ValueError("unsupported AUTO work kind")
        subject = await session.get(model, record_id)
        if subject is None:
            return "NOT_FOUND"
        task = await session.get(AutoTask, subject.task_id) if subject.task_id else None
        if task is None and kind != "BIN":
            return "NOT_FOUND"
        line_id = subject.work_line_id if kind == "BIN" else task.work_line_id
        await lock_line(session, self.dispatch, line_id, roller=self.roller)
        await session.refresh(subject)
        configured = await self._configured(session, line_id)
        if kind == "TASK":
            if task.status == "QUEUED":
                if configured:
                    return await self.prepare_next(session, work_line_id=line_id, now_ms=now_ms)
                return "WAITING"
            if (task.status == "CANCELLED" and task.pending) or (
                task.status == "PREPARING" and task.prepare_accepted_at_ms is None
            ):
                await self.advance_prepare(session, task_id=record_id, now_ms=now_ms)
            if configured:
                inbound, outbound = self.rack_flows[line_id]
                flow = inbound if task.business_type == "inbound" else outbound
                await flow.advance(session, task_id=record_id, now_ms=now_ms)
            return await self.task_flow.advance(
                session, task_id=record_id, now_ms=now_ms, allow_new=configured
            )
        if kind == "BIN":
            await self.work_plans.consume_bin(
                session, work_line_id=line_id, arrival_id=subject.arrival_id, now_ms=now_ms
            )
            if configured:
                await self.work_plans.start_bin(
                    session, work_line_id=line_id, arrival_id=subject.arrival_id, now_ms=now_ms
                )
            if not configured:
                return "WAITING"
            return await self.task_flow.start_release(
                session, work_line_id=line_id, arrival_id=subject.arrival_id, now_ms=now_ms
            )
        if kind == "OUTBOUND_FACE":
            await self.work_plans.consume_return_face(
                session, work_line_id=line_id, member_id=record_id, now_ms=now_ms
            )
            if configured:
                return await self.work_plans.start_return_face(
                    session, work_line_id=line_id, member_id=record_id, now_ms=now_ms
                )
            return "WAITING"
        flow = self.inbound if kind == "INBOUND_PLATE" else self.outbound
        return await flow.advance(session, work_line_id=line_id, plate_id=record_id, now_ms=now_ms)
