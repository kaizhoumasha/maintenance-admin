"""AUTO-owned production projection; direction requests and releases remain host work."""

from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.bin_roller.ports import (
    CurrentTask,
    Scan2Arrival,
    SourceFaceRef,
    SuccessorContext,
)
from app.businesses._components.rack_dispatch.contracts import Coverage, FactRef, PointRef
from app.businesses._components.sorting_task.contracts import PrepareAccepted
from app.core.clock import unix_ms
from app.integrations.ecs_callback import EcsEventContext
from app.models import WorkLine

from .models import AutoBinWork, AutoSupplyMember, AutoTask, AutoTaskInput, AutoTaskRequest


async def matching_arrival(roller, session, work):
    try:
        original = await roller.original_arrival(session, work.arrival_id)
    except ValueError:
        return False
    return (
        original.get("scope_kind") == ("TASK" if work.task_id is not None else "LINE")
        and original.get("task_id") == work.task_id
        and original.get("production_record_id") == work.arrival_id
        and (
            original["id"],
            original["work_line_id"],
            original["device_code"],
            original["raw_bin_code"],
            original["bin_code"],
            original["scanned_at"],
        )
        == (
            work.arrival_id,
            work.work_line_id,
            work.device_code,
            work.raw_bin_code,
            work.bin_code,
            work.scanned_at,
        )
    )


class RollerPorts:
    def __init__(self, *, work_plans=None, task_flow=None):
        self.work_plans = work_plans
        self.task_flow = task_flow

    async def read_current_task(self, session: AsyncSession, *, work_line_id: int) -> CurrentTask:
        task = await session.scalar(
            select(AutoTask).where(
                AutoTask.work_line_id == work_line_id,
                AutoTask.status.in_(("PREPARING", "EXECUTING")),
            )
        )
        line = await session.get(WorkLine, work_line_id, populate_existing=True)
        valid = bool(
            task
            and line
            and line.is_enabled
            and line.business_code == "smt_auto"
            and line.line_code == task.workline_code
            and await self.prepared(session, task)
        )
        return CurrentTask(task.task_id if task else None, valid)

    @staticmethod
    async def prepared(session, task):
        if task.prepare_accepted_at_ms is None:
            return False
        preparations = tuple(
            await session.scalars(
                select(AutoTaskRequest).where(
                    AutoTaskRequest.task_id == task.task_id, AutoTaskRequest.purpose == "PREPARE"
                )
            )
        )
        if len(preparations) != 1:
            return False
        prepare = preparations[0]
        if (
            prepare.operation != "sorting.task.prepare@v1"
            or prepare.unresolved
            or prepare.consumed_at_ms is None
            or prepare.request_data
            != {"task_id": task.task_id, "workline_code": task.workline_code}
        ):
            return False
        response = prepare.response_snapshot or {}
        try:
            PrepareAccepted.parse(
                response.get("http_status"), response.get("code"), response.get("data")
            )
        except (ValueError, TypeError):
            return False
        return True

    async def resolve_scan2_task(self, session: AsyncSession, *, work_line_id: int) -> CurrentTask:
        await session.execute(
            select(AutoTask.task_id)
            .where(
                AutoTask.work_line_id == work_line_id,
                AutoTask.status.in_(("PREPARING", "EXECUTING")),
            )
            .order_by(AutoTask.task_id)
            .with_for_update()
        )
        return await self.read_current_task(session, work_line_id=work_line_id)

    async def read_successor(self, session: AsyncSession, *, work_line_id: int) -> SuccessorContext:
        current = await self.read_current_task(session, work_line_id=work_line_id)
        if current.task_id:
            preparations = tuple(
                await session.scalars(
                    select(AutoTaskRequest).where(
                        AutoTaskRequest.task_id == current.task_id,
                        AutoTaskRequest.purpose == "PREPARE",
                    )
                )
            )
            prepare = preparations[0] if len(preparations) == 1 else None
            return SuccessorContext(
                "AVAILABLE" if current.is_valid else "CHECKING",
                {
                    "task_id": current.task_id,
                    "prepare_operation_id": prepare.operation_id if prepare else None,
                },
            )
        queued = tuple(
            await session.scalars(
                select(AutoTask.task_id)
                .where(AutoTask.work_line_id == work_line_id, AutoTask.status == "QUEUED")
                .order_by(AutoTask.dispatch_sequence, AutoTask.issued_timestamp, AutoTask.task_id)
            )
        )
        return SuccessorContext(
            "CHECKING" if queued else "UNKNOWN", {"queued_task_ids": list(queued)}
        )

    async def read_supply_order(
        self, session: AsyncSession, *, work_line_id: int, task_id: str
    ) -> tuple[SourceFaceRef, ...]:
        rows = await session.scalars(
            select(AutoSupplyMember)
            .join(AutoTask, AutoTask.task_id == AutoSupplyMember.task_id)
            .where(AutoTask.work_line_id == work_line_id, AutoTask.task_id == task_id)
            .order_by(
                AutoSupplyMember.plan_revision,
                AutoSupplyMember.rack_array_index,
                AutoSupplyMember.face_array_index,
                AutoSupplyMember.id,
            )
        )
        return tuple(SourceFaceRef(row.plan_revision, row.rack_id, row.rack_face) for row in rows)

    async def read_task_coverage(
        self, session: AsyncSession, *, point: PointRef, task_id: str
    ) -> Coverage | None:
        task = await session.get(AutoTask, task_id, populate_existing=True)
        if (
            point.owner_domain != "smt_auto"
            or task is None
            or task.work_line_id != point.work_line_id
        ):
            return None
        inputs = tuple(
            await session.scalars(
                select(AutoTaskInput)
                .where(AutoTaskInput.task_id == task_id)
                .order_by(AutoTaskInput.id)
            )
        )
        return Coverage(
            "task_inputs",
            (
                "smt_auto",
                str(point.work_line_id),
                point.job_id,
                point.rack_id,
                point.point,
                task_id,
            ),
            FactRef("task", (task_id,), (str(task.plan_revision), task.status)),
            tuple(
                FactRef(
                    "task_input",
                    (str(row.id), row.operation_id),
                    (row.disposition, str(row.applied_plan_revision), row.effective_digest),
                )
                for row in inputs
            ),
        )

    async def confirmation_pending(self, session, *, task_id):
        requests = tuple(
            await session.scalars(
                select(AutoTaskRequest)
                .where(AutoTaskRequest.task_id == task_id, AutoTaskRequest.purpose == "COMPLETION")
                .order_by(AutoTaskRequest.id)
            )
        )
        if any(request.unresolved for request in requests):
            return True
        if not requests:
            return False
        decision = requests[-1].completion_result or {}
        task = await session.get(AutoTask, task_id)
        return decision.get("result") == "PLAN_REVISION_STALE" and (
            task is None or task.plan_revision < decision["current_plan_revision"]
        )

    async def accept_scan2(self, ctx: EcsEventContext, arrival: Scan2Arrival) -> str:
        session = ctx.session
        original = await session.scalar(
            select(AutoBinWork).where(
                AutoBinWork.work_line_id == arrival.work_line_id,
                AutoBinWork.arrival_id == arrival.arrival_id,
            )
        )
        if original is not None:
            return original.arrival_id
        current = await self.read_current_task(session, work_line_id=arrival.work_line_id)
        task = await session.get(AutoTask, current.task_id) if current.task_id else None
        reason = None
        exception_basis = None
        if arrival.bin_code is not None:
            originals = (
                await self.task_flow.original_inprocess(
                    session, work_line_id=arrival.work_line_id, bin_code=arrival.bin_code
                )
                if self.task_flow is not None
                else tuple(
                    await session.scalars(
                        select(AutoBinWork)
                        .where(
                            AutoBinWork.work_line_id == arrival.work_line_id,
                            AutoBinWork.bin_code == arrival.bin_code,
                        )
                        .order_by(AutoBinWork.id)
                    )
                )
            )
            if originals:
                owners = {row.task_id for row in originals}
                exception_basis = {
                    "original_arrival_ids": [row.arrival_id for row in originals],
                    "observed_current_task_id": current.task_id,
                }
                task = (
                    await session.get(AutoTask, next(iter(owners)))
                    if len(owners) == 1 and None not in owners
                    else None
                )
                reason = "ORIGINAL_INPROCESS"
        if (
            arrival.bin_code is None
            or not arrival.raw_bin_code.endswith("-C")
            or type(arrival.scanned_at) is not int
            or not 0 <= arrival.scanned_at <= 9223372036854775807
        ):
            reason = "INVALID_SCAN"
        elif reason is not None:
            pass
        elif task is not None and not current.is_valid:
            reason = "CONTEXT_NOT_READY"
        elif task is not None:
            if await self.confirmation_pending(session, task_id=task.task_id):
                reason = "CONFIRMATION_PENDING"
        association = "TASK" if task else "EXCEPTION" if reason else "NO_TASK"
        session.add(
            AutoBinWork(
                **asdict(arrival),
                task_id=task.task_id if task else None,
                business_type=task.business_type if task else None,
                association=association,
                production_hold_reason=reason,
                exception_basis=exception_basis,
                pending=reason in (None, "CONFIRMATION_PENDING"),
            )
        )
        await session.flush()
        if self.work_plans is not None and association == "TASK" and reason is None:
            await self.work_plans.start_bin(
                session,
                work_line_id=arrival.work_line_id,
                arrival_id=arrival.arrival_id,
                now_ms=unix_ms(datetime.now(UTC)),
            )
        return arrival.arrival_id
