import json
from time import time
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.bin_roller.ports import (
    CurrentTask,
    Scan2Arrival,
    SourceFaceRef,
    SuccessorContext,
)
from app.businesses._components.rack_dispatch.contracts import Coverage, FactRef, PointRef
from app.integrations.ecs_callback import EcsEventContext
from app.models import WorkLine

from .repository import TaskRepository

if TYPE_CHECKING:
    from .manual_bin_flow import ManualBinFlow


class RollerPorts:
    def __init__(
        self, repository: TaskRepository, *, manual_bins: "ManualBinFlow | None" = None
    ) -> None:
        self.repository = repository
        self.manual_bins = manual_bins

    async def resolve_scan2_task(self, session: AsyncSession, *, work_line_id: int) -> CurrentTask:
        # The event resolver owns the line and source device before any scan row is written.
        table = self.repository.tables["task"]
        await session.execute(
            select(table.c.task_id)
            .where(
                table.c.work_line_id == work_line_id, table.c.status.in_(("PREPARING", "EXECUTING"))
            )
            .order_by(table.c.task_id)
            .with_for_update()
        )

        return await self.read_current_task(session, work_line_id=work_line_id)

    async def accept_scan2(self, ctx: EcsEventContext, arrival: Scan2Arrival) -> str:
        if self.manual_bins is None:
            raise RuntimeError("manual bin production must be assembled")
        return await self.manual_bins.accept_scan2(ctx, arrival)

    async def read_task_coverage(
        self, session: AsyncSession, *, point: PointRef, task_id: str
    ) -> Coverage | None:
        task = await self.repository.task(session, task_id)
        if (
            point.owner_domain != "smt_manual"
            or not task
            or task["work_line_id"] != point.work_line_id
        ):
            return None
        inputs = self.repository.tables["input"]
        pending = (
            await session.execute(
                select(inputs)
                .where(
                    inputs.c.task_id == task_id,
                    inputs.c.work_line_id == point.work_line_id,
                    inputs.c.state.in_(("DEFERRED", "PENDING")),
                    inputs.c.operation.in_(
                        ("sorting.task.plan_delta@v1", "sorting.task.cancel@v1")
                    ),
                )
                .order_by(*inputs.primary_key)
            )
        ).mappings()
        return Coverage(
            "task_inputs",
            (
                point.owner_domain,
                str(point.work_line_id),
                point.job_id,
                point.rack_id,
                point.point,
                task_id,
            ),
            FactRef(
                "task",
                (task_id,),
                (
                    str(task["plan_revision"]),
                    task["status"],
                    json.dumps(task["received_work"], sort_keys=True),
                ),
            ),
            tuple(
                FactRef(
                    "pending_input",
                    (r["operation"], r["operation_id"]),
                    (r["state"], str(r["plan_revision"]), json.dumps(r["payload"], sort_keys=True)),
                )
                for r in pending
            ),
        )

    async def read_current_task(self, session: AsyncSession, *, work_line_id: int) -> CurrentTask:
        table = self.repository.tables["task"]
        row = (
            (
                await session.execute(
                    select(table).where(
                        table.c.work_line_id == work_line_id,
                        table.c.status.in_(("PREPARING", "EXECUTING")),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        line = await session.get(WorkLine, work_line_id, populate_existing=True)
        valid = bool(
            row
            and row["prepare_accepted"]
            and line
            and line.is_enabled
            and line.business_code == "smt_manual"
            and line.line_code == row["workline_code"]
        )
        return CurrentTask(row["task_id"] if row else None, valid)

    async def read_successor(self, session: AsyncSession, *, work_line_id: int) -> SuccessorContext:
        table = self.repository.tables["task"]
        line = await session.get(WorkLine, work_line_id, populate_existing=True)
        if line is None or not line.is_enabled or line.business_code != "smt_manual":
            return SuccessorContext("UNKNOWN", dict(work_line_id=work_line_id))
        rows = (
            (
                await session.execute(
                    select(table)
                    .where(
                        table.c.work_line_id == work_line_id,
                        table.c.status.in_(("QUEUED", "PREPARING", "EXECUTING")),
                    )
                    .order_by(table.c.dispatch_sequence, table.c.issued_timestamp, table.c.task_id)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            if row["status"] in {"PREPARING", "EXECUTING"}:
                return SuccessorContext(
                    "AVAILABLE" if row["prepare_accepted"] else "CHECKING",
                    dict(task_id=row["task_id"], prepare_operation_id=row["prepare_operation_id"]),
                )
        eligible = [
            r["task_id"]
            for r in rows
            if r["not_before"] is None or r["not_before"] <= int(time() * 1000)
        ]
        if not rows:
            confirmation = self.repository.tables["request"]
            sealed = (
                await session.execute(
                    select(table.c.task_id, confirmation.c.operation_id)
                    .join(
                        confirmation,
                        confirmation.c.operation_id == table.c.confirmation_operation_id,
                    )
                    .where(
                        table.c.work_line_id == work_line_id,
                        table.c.status == "COMPLETED",
                        confirmation.c.kind == "COMPLETION_CONFIRM",
                        confirmation.c.task_id == table.c.task_id,
                        confirmation.c.work_line_id == table.c.work_line_id,
                        confirmation.c.decision_code == "COMPLETED",
                        confirmation.c.state == "CONSUMED",
                    )
                    .order_by(table.c.issued_timestamp.desc(), table.c.task_id.desc())
                    .limit(1)
                )
            ).one_or_none()
            if sealed is not None:
                return SuccessorContext(
                    "NONE",
                    dict(task_id=sealed.task_id, confirmation_operation_id=sealed.operation_id),
                )
            inputs = self.repository.tables["input"]
            cancelled = (
                await session.execute(
                    select(table.c.task_id, inputs.c.operation_id, inputs.c.payload)
                    .join(inputs, inputs.c.task_id == table.c.task_id)
                    .where(
                        table.c.work_line_id == work_line_id,
                        table.c.status == "CANCELLED",
                        inputs.c.operation == "sorting.task.cancel@v1",
                        inputs.c.state == "APPLIED",
                    )
                    .order_by(table.c.issued_timestamp.desc(), table.c.task_id.desc())
                )
            ).all()
            for original in cancelled:
                if original.payload["cancel_scope"] == "TASK":
                    return SuccessorContext(
                        "NONE",
                        dict(task_id=original.task_id, cancel_operation_id=original.operation_id),
                    )
        return SuccessorContext(
            "CHECKING" if eligible else "UNKNOWN", dict(queued_task_ids=eligible)
        )

    async def allows_task_entry(
        self, session: AsyncSession, *, work_line_id: int, task_id: str
    ) -> bool:
        current = await self.read_current_task(session, work_line_id=work_line_id)
        if not current.is_valid or current.task_id != task_id:
            return False
        return not await self.repository.has_pending_plan(session, task_id)

    async def read_supply_order(
        self, session: AsyncSession, *, work_line_id: int, task_id: str
    ) -> tuple[SourceFaceRef, ...]:
        task = await self.repository.task(session, task_id)
        if not task or task["work_line_id"] != work_line_id:
            return ()
        members = self.repository.tables["member"]
        rows = (
            await session.execute(
                select(members)
                .where(members.c.task_id == task_id, members.c.kind == "BIN")
                .order_by(members.c.plan_revision, members.c.member_order)
            )
        ).mappings()
        return tuple(SourceFaceRef(r["plan_revision"], r["rack_id"], r["rack_face"]) for r in rows)
