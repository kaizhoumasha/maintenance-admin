from collections.abc import Mapping

from sqlalchemy import Table, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.integrations.wms_callback import CallbackContext
from app.models import Device, WorkLine
from app.services.workline_service import lock_worklines

from .models import TABLES


class TaskRepository:
    def __init__(self, *, tables: Mapping[str, Table] | None = None) -> None:
        self.tables = TABLES if tables is None else tables

    async def stop_bin_scheduling(self, session, task_id):
        table = self.tables["manual_bin"]
        await session.execute(
            select(table.c.arrival_id)
            .where(table.c.task_id == task_id)
            .order_by(table.c.arrival_id)
            .with_for_update()
        )
        await session.execute(
            table.update().where(table.c.task_id == task_id).values(pending=False)
        )

    async def task(
        self, session: AsyncSession, task_id: str, *, lock: bool = False
    ) -> RowMapping | None:
        table = self.tables["task"]
        query = select(table).where(table.c.task_id == task_id)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one_or_none()

    async def lock_line(self, session: AsyncSession, work_line_id: int) -> WorkLine | None:
        try:
            line = (await lock_worklines(session, [work_line_id]))[0]
        except AppError as error:
            if error.code == ErrorCode.NOT_FOUND:
                return None
            raise
        await session.scalars(
            select(Device)
            .where(Device.work_line_id == line.id)
            .order_by(Device.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return line

    async def lock_dispatch_line(
        self, session, roller, work_line_id, *, task_id=None, lock_actions=True
    ):
        line = await self.lock_line(session, work_line_id)
        from app.businesses._components.bin_roller.service import ROLLER_SOURCES, RackLockSetChanged

        from .manual_rack_flow import MANUAL_SOURCES

        kinds = (*ROLLER_SOURCES, *MANUAL_SOURCES)
        admission = await roller.rack_dispatch.admission_jobs(session, work_line_id=work_line_id)
        await roller.rack_dispatch.lock_admission_jobs(
            session, job_ids=tuple(j.id for j in admission)
        )
        jobs = await roller.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=kinds, pending=True
        )
        task_ids = {job.origin.task_id for job in jobs if job.origin.task_id}
        if task_id is not None:
            task_ids.add(task_id)
        await self.lock_dispatch_subjects(session, work_line_id, task_ids)
        await roller.lock_production_subjects(session, work_line_id, jobs)
        confirmed = await roller.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=kinds, pending=True
        )
        confirmed_admission = await roller.rack_dispatch.admission_jobs(
            session, work_line_id=work_line_id
        )
        if tuple(j.id for j in admission) != tuple(j.id for j in confirmed_admission) or tuple(
            j.id for j in jobs
        ) != tuple(j.id for j in confirmed):
            raise RackLockSetChanged("original dispatch jobs changed during lock acquisition")
        if lock_actions:
            await roller.rack_dispatch.lock_admission_actions(
                session, job_ids=tuple(job.id for job in admission)
            )
        return line, jobs

    async def lock_dispatch_subjects(self, session, work_line_id, task_ids):
        tasks = self.tables["task"]
        active = select(tasks.c.task_id).where(
            tasks.c.work_line_id == work_line_id,
            tasks.c.status.in_(("QUEUED", "PREPARING", "EXECUTING"))
            | tasks.c.task_id.in_(task_ids),
        )
        for key in ("task", "input", "member", "manual_bin", "request"):
            table = self.tables[key]
            condition = table.c.task_id.in_(active)
            if key == "manual_bin":
                condition |= (table.c.work_line_id == work_line_id) & (
                    table.c.pending | (table.c.state == "WORK_REQUIRED")
                )
            await session.execute(
                select(*table.primary_key)
                .where(condition)
                .order_by(*table.primary_key)
                .with_for_update()
            )

    async def original_dispatch_task(self, session, roller, task_id):
        located = await self.task(session, task_id)
        if located is None:
            return None, None, ()
        line, jobs = await self.lock_dispatch_line(
            session, roller, located["work_line_id"], task_id=task_id, lock_actions=False
        )
        return line, await self.task(session, task_id), jobs

    async def original(
        self, session: AsyncSession, task_id: str
    ) -> tuple[WorkLine | None, RowMapping | None]:
        located = await self.task(session, task_id)
        if located is None:
            return None, None
        line = await self.lock_line(session, located["work_line_id"])
        return line, await self.task(session, task_id, lock=True)

    async def save_task(self, session: AsyncSession, task_id: str, **values) -> None:
        table = self.tables["task"]
        await session.execute(table.update().where(table.c.task_id == task_id).values(**values))

    async def input(
        self, session: AsyncSession, operation: str, operation_id: str
    ) -> RowMapping | None:
        table = self.tables["input"]
        return (
            (
                await session.execute(
                    select(table).where(
                        table.c.operation == operation, table.c.operation_id == operation_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def save_input(
        self, ctx: CallbackContext, payload, *, applied: bool, reason: str | None = None
    ) -> None:
        table = self.tables["input"]
        values = dict(
            state="APPLIED"
            if applied
            else "DEFERRED"
            if reason in {"PREPARE_PENDING", "MISSING_REVISION", "WORKLINE_DISABLED"}
            else "REJECTED",
            reason=reason,
        )
        if await self.input(ctx.session, ctx.operation, ctx.operation_id) is None:
            await ctx.session.execute(
                table.insert().values(
                    operation_id=ctx.operation_id,
                    task_id=payload.task_id,
                    operation=ctx.operation,
                    timestamp=ctx.timestamp,
                    payload=payload.model_dump(mode="json"),
                    receipt_id=ctx.receipt_id,
                    work_line_id=(await self.task(ctx.session, payload.task_id))["work_line_id"],
                    plan_revision=getattr(payload, "plan_revision", None),
                    **values,
                )
            )
        else:
            await ctx.session.execute(
                table.update()
                .where(table.c.operation == ctx.operation, table.c.operation_id == ctx.operation_id)
                .values(**values)
            )

        if ctx.operation in {"sorting.task.plan_delta@v1", "sorting.task.cancel@v1"}:
            task = await self.task(ctx.session, payload.task_id)
            if task is not None:
                await self.note_received_work(
                    ctx.session, task, operation=ctx.operation, record_id=ctx.operation_id
                )

    async def line_by_code(self, session: AsyncSession, code: str) -> WorkLine | None:
        line_id = await session.scalar(select(WorkLine.id).where(WorkLine.line_code == code))
        return None if line_id is None else await self.lock_line(session, line_id)

    async def bin_release_evidence(self, session, roller, work):
        """Read the original permission and SCAN2 release intent without waiting for its result."""
        from app.businesses._components.bin_roller.completion import release_intent
        from app.businesses._components.bin_roller.ports import Scan2Arrival
        from app.businesses._components.rack_dispatch.contracts import FactRef

        from .contracts import ADMISSION_DECISION
        from .models import BIN_COMPLETION_OPERATION
        from .request.manual_bin_work_admission_decide_v1 import OPERATION

        if (
            work["state"] != "RELEASED"
            or not work["admission_operation_id"]
            or not work["command_code"]
        ):
            return None
        requests = self.tables["request"]
        original = (
            (
                await session.execute(
                    select(requests).where(
                        requests.c.operation == OPERATION,
                        requests.c.operation_id == work["admission_operation_id"],
                        requests.c.kind == "BIN_ADMISSION",
                        requests.c.task_id == work["task_id"],
                        requests.c.work_line_id == work["work_line_id"],
                        requests.c.arrival_id == work["arrival_id"],
                        requests.c.state == "CONSUMED",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if not original or original["request_snapshot"] != {
            k: work[k] for k in ("task_id", "bin_code", "scanned_at")
        }:
            return None
        response = original["response"] or {}
        if (
            response.get("code") != "DECIDED"
            or type(response.get("timestamp")) is not int
            or response["timestamp"] <= 0
        ):
            return None
        try:
            decision = ADMISSION_DECISION.validate_python(response.get("data"))
        except ValueError:
            return None
        if decision.result != original["decision_code"]:
            return None
        notices = self.tables["input"]
        applied = (
            (
                await session.execute(
                    select(notices)
                    .where(
                        notices.c.operation == BIN_COMPLETION_OPERATION,
                        notices.c.arrival_id == work["arrival_id"],
                        notices.c.state == "APPLIED",
                    )
                    .limit(2)
                )
            )
            .mappings()
            .all()
        )
        if decision.result == "NO_WORK":
            if applied or work["completed_operation_id"] or work["completed_result"]:
                return None
        elif decision.result == "WORK_REQUIRED":
            if not work["completed_operation_id"] or decision.task_id != work["task_id"]:
                return None
            if len(applied) != 1:
                return None
            completion = applied[0]
            if (
                completion["operation_id"] != work["completed_operation_id"]
                or any(
                    completion[k] != work[k]
                    for k in ("work_line_id", "task_id", "arrival_id", "bin_code")
                )
                or completion["completion_result"] != work["completed_result"]
                or work["completed_result"] not in ("NORMAL", "NG")
            ):
                return None
        else:
            return None
        scan = await release_intent(
            roller,
            session,
            arrival=Scan2Arrival(
                work["arrival_id"],
                work["work_line_id"],
                work["device_code"],
                work["raw_bin_code"],
                work["bin_code"],
                work["scanned_at"],
            ),
            task_id=work["task_id"],
            command_code=work["command_code"],
        )
        if scan is None:
            return None
        return FactRef(
            "scan2_release",
            (scan["id"], scan["production_record_id"]),
            (
                f"bin_roller:scan2_release:{scan['id']}",
                work["command_code"],
                original["operation_id"],
                str(original["message_id"]),
                decision.result,
                str(work["completed_operation_id"]),
                str(work["completed_result"]),
            ),
        )

    async def confirmation(self, session: AsyncSession, task: RowMapping) -> RowMapping | None:
        table = self.tables["request"]
        return (
            (
                await session.execute(
                    select(table).where(
                        table.c.kind == "COMPLETION_CONFIRM",
                        table.c.operation_id == task["confirmation_operation_id"],
                        table.c.task_id == task["task_id"],
                        table.c.work_line_id == task["work_line_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def has_pending_plan(self, session: AsyncSession, task_id: str) -> bool:
        table = self.tables["input"]
        applied = table.alias("applied_plan")
        return bool(
            await session.scalar(
                select(table.c.operation_id)
                .where(
                    table.c.task_id == task_id,
                    table.c.operation == "sorting.task.plan_delta@v1",
                    table.c.state == "DEFERRED",
                    table.c.reason.in_(
                        ("PREPARE_PENDING", "MISSING_REVISION", "WORKLINE_DISABLED")
                    ),
                    ~select(applied.c.operation_id)
                    .where(
                        applied.c.task_id == table.c.task_id,
                        applied.c.operation == table.c.operation,
                        applied.c.plan_revision == table.c.plan_revision,
                        applied.c.state == "APPLIED",
                    )
                    .exists(),
                )
                .limit(1)
            )
        )

    async def confirmation_blocks_admission(self, session: AsyncSession, task: RowMapping) -> bool:
        confirmation = await self.confirmation(session, task)
        if confirmation is None:
            return task["confirmation_operation_id"] is not None
        if (
            confirmation["state"] in {"REQUESTED", "CONFLICT"}
            or confirmation["decision_code"] == "COMPLETED"
        ):
            return True
        if confirmation["decision_code"] == "PLAN_REVISION_STALE":
            return task["plan_revision"] < confirmation["response"]["data"]["current_plan_revision"]
        return False

    async def note_received_work(
        self, session: AsyncSession, task: RowMapping, *, operation: str, record_id: str
    ) -> None:
        reference = [operation, record_id]
        if reference in task["received_work"]:
            return
        # Owning facts invalidate old snapshots before registration as well as response consumption.
        await self.save_task(
            session, task["task_id"], received_work=[*task["received_work"], reference]
        )
