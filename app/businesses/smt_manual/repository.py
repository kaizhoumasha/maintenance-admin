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
        jobs = await roller.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=kinds, pending=True
        )
        await roller.rack_dispatch.lock_jobs(session, job_ids=tuple(j.id for j in jobs))
        task_ids = {job.origin.task_id for job in jobs if job.origin.task_id}
        if task_id is not None:
            task_ids.add(task_id)
        await self.lock_dispatch_subjects(session, work_line_id, task_ids)
        await roller.lock_production_subjects(session, work_line_id, jobs)
        confirmed = await roller.rack_dispatch.jobs(
            session, work_line_id=work_line_id, source_kinds=kinds, pending=True
        )
        if tuple(j.id for j in jobs) != tuple(j.id for j in confirmed):
            raise RackLockSetChanged("original dispatch jobs changed during lock acquisition")
        if lock_actions:
            await roller.rack_dispatch.lock_actions(session, job_ids=tuple(job.id for job in jobs))
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
