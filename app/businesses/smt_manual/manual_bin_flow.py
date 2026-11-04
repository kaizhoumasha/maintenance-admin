"""Caller-owned transactions for original SCAN2 admission and completion evidence."""

from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.businesses._components.bin_roller.ports import Scan2Arrival
from app.businesses._components.bin_roller.service import BinRoller
from app.businesses.runtime import Capabilities
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import EcsEventContext
from app.integrations.wms_callback import Accepted, CallbackContext, Outcome, Rejected, Unavailable
from app.models import WorkLine
from app.reliable import ScopeRef

from .contracts import ADMISSION_DECISION, ManualCompleted
from .manual_rack_flow import ManualRackFlow
from .models import BIN_COMPLETION_OPERATION
from .repository import TaskRepository
from .request.manual_bin_work_admission_decide_v1 import OPERATION, AdmissionRequest


class ManualBinFlow:
    def __init__(
        self, capabilities: Capabilities, roller: BinRoller, *, repository: TaskRepository
    ) -> None:
        self.capabilities = capabilities
        self.roller = roller
        self.repository = repository
        self.tables = repository.tables
        self.manual_racks = ManualRackFlow(capabilities, roller, repository=repository)

    def _pending(self, kind: str) -> tuple[ColumnElement[str], ColumnElement[bool]]:
        if kind == "MANUAL_BIN":
            table = self.tables["manual_bin"]
            return table.c.arrival_id, table.c.pending.is_(True)
        if kind == "MANUAL_COMPLETION":
            table = self.tables["input"]
            return table.c.operation_id, (table.c.operation == BIN_COMPLETION_OPERATION) & (
                table.c.state == "PENDING"
            )
        raise ValueError("invalid manual pending kind")

    async def get_pending_upper_id(self, session: AsyncSession, *, kind: str) -> str | None:
        identity, pending = self._pending(kind)
        return await session.scalar(
            select(identity).where(pending).order_by(identity.desc()).limit(1)
        )

    async def list_pending(
        self,
        session: AsyncSession,
        *,
        kind: str,
        after_id: str | None,
        upper_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        identity, pending = self._pending(kind)
        query = select(identity).where(pending, identity <= upper_id)
        if after_id is not None:
            query = query.where(identity > after_id)
        return tuple(await session.scalars(query.order_by(identity).limit(limit)))

    async def _one(
        self, session: AsyncSession, key: str, identity: ColumnElement[bool], *, lock: bool = False
    ) -> RowMapping | None:
        table = self.tables[key]
        query = select(table).where(identity)
        if key == "input":
            query = query.where(table.c.operation == BIN_COMPLETION_OPERATION)
        elif key == "request":
            query = query.where(table.c.kind == "BIN_ADMISSION")
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).mappings().one_or_none()

    async def _save(
        self, session: AsyncSession, key: str, identity: ColumnElement[bool], **values
    ) -> None:
        table = self.tables[key]
        if key == "input":
            identity = identity & (table.c.operation == BIN_COMPLETION_OPERATION)
        elif key == "request":
            identity = identity & (table.c.kind == "BIN_ADMISSION")
        await session.execute(table.update().where(identity).values(**values))

    async def _request(self, session: AsyncSession, work: RowMapping | dict) -> None:
        operation_id = str(uuid7())
        message = await self.capabilities.wms.send(
            session,
            AdmissionRequest(
                task_id=work["task_id"], bin_code=work["bin_code"], scanned_at=work["scanned_at"]
            ),
            operation_id=operation_id,
            scope=ScopeRef(self.roller.context.domain, "TASK", work["task_id"]),
        )
        await session.execute(
            self.tables["request"]
            .insert()
            .values(
                kind="BIN_ADMISSION",
                operation=OPERATION,
                operation_id=operation_id,
                task_id=work["task_id"],
                work_line_id=work["work_line_id"],
                arrival_id=work["arrival_id"],
                previous_operation_id=work["admission_operation_id"],
                request_snapshot=dict(
                    task_id=work["task_id"],
                    bin_code=work["bin_code"],
                    scanned_at=work["scanned_at"],
                ),
                message_id=message.message_id,
                response=None,
                saved_at_ms=None,
                due_at_ms=None,
            )
        )
        table = self.tables["manual_bin"]
        await self._save(
            session,
            "manual_bin",
            table.c.arrival_id == work["arrival_id"],
            admission_operation_id=operation_id,
            state="ADMISSION_PENDING",
            pending=True,
        )

    async def accept_scan2(self, ctx: EcsEventContext, arrival: Scan2Arrival) -> str:
        table = self.tables["manual_bin"]
        # The ECS binding resolver already holds the original line and source device.
        if await self._one(ctx.session, "manual_bin", table.c.arrival_id == arrival.arrival_id):
            return arrival.arrival_id
        current = await self.roller.production.read_current_task(
            ctx.session, work_line_id=arrival.work_line_id
        )
        reason = None
        now_ms = unix_ms(self.capabilities.wms.clock())
        if arrival.bin_code is None or not arrival.raw_bin_code.endswith("-C"):
            reason = "INVALID_SCAN"
        elif not 0 < arrival.scanned_at <= now_ms:
            reason = "INVALID_SCAN_TIME"
        elif not current.is_valid or current.task_id is None:
            reason = "NO_CONTEXT"
        task = await self.repository.task(ctx.session, current.task_id) if current.task_id else None
        deferred = bool(
            reason is None
            and task is not None
            and await self.repository.confirmation_blocks_admission(ctx.session, task)
        )
        values = dict(
            **asdict(arrival),
            task_id=current.task_id,
            state="HOLD" if reason else "DEFERRED" if deferred else "ADMISSION_PENDING",
            reason=reason or ("CONFIRMATION_PENDING" if deferred else None),
            pending=reason is None,
            admission_operation_id=None,
            completed_operation_id=None,
            completed_result=None,
            command_code=None,
        )
        await ctx.session.execute(table.insert().values(**values))
        if task is not None:
            await self.repository.note_received_work(
                ctx.session, task, operation="SCAN2", record_id=arrival.arrival_id
            )
        if reason is None and not deferred:
            await self._request(ctx.session, values)
        return arrival.arrival_id

    async def advance_bin(
        self, session: AsyncSession, *, arrival_id: str, now_ms: int, locked: bool = False
    ) -> str:
        table = self.tables["manual_bin"]
        located = await self._one(session, "manual_bin", table.c.arrival_id == arrival_id)
        if located is None:
            return "NOT_FOUND"
        if not locked:
            await self.repository.lock_dispatch_line(
                session, self.roller, located["work_line_id"], task_id=located["task_id"]
            )
        line = await session.get(WorkLine, located["work_line_id"])
        work = await self._one(session, "manual_bin", table.c.arrival_id == arrival_id, lock=True)
        if work["task_id"]:
            task = await self.repository.task(session, work["task_id"])
            if task is None or task["status"] in ("COMPLETED", "CANCELLED"):
                return "CLOSED"
        if work["state"] == "DEFERRED":
            task = await self.repository.task(session, work["task_id"])
            current = await self.roller.production.read_current_task(
                session, work_line_id=work["work_line_id"]
            )
            if (
                task is None
                or not current.is_valid
                or current.task_id != work["task_id"]
                or await self.repository.has_pending_plan(session, work["task_id"])
                or await self.repository.confirmation_blocks_admission(session, task)
            ):
                return "DEFERRED"
            await self._request(session, work)
            await self._save(session, "manual_bin", table.c.arrival_id == arrival_id, reason=None)
            return "ADMISSION_PENDING"
        if work["state"] not in {"ADMISSION_PENDING", "WAIT"}:
            return work["state"]
        admissions = self.tables["request"]
        original = await self._one(
            session,
            "request",
            (admissions.c.operation_id == work["admission_operation_id"])
            & (admissions.c.arrival_id == arrival_id)
            & (admissions.c.task_id == work["task_id"])
            & (admissions.c.work_line_id == work["work_line_id"]),
            lock=True,
        )
        if original is None:
            return work["state"]
        if work["state"] == "WAIT":
            if now_ms >= original["due_at_ms"]:
                task = await self.repository.task(session, work["task_id"])
                if (
                    line is None
                    or not line.is_enabled
                    or line.business_code != "smt_manual"
                    or task is None
                    or line.line_code != task["workline_code"]
                ):
                    return "WAIT"
                await self._request(session, work)
                return "ADMISSION_PENDING"
            return "WAIT"
        result = await self.capabilities.wms.get_result(
            session,
            message_id=original["message_id"],
            operation=OPERATION,
            operation_id=original["operation_id"],
        )
        if (
            result.availability != "FOUND"
            or result.response_state != "VALID"
            or result.http_status != 200
            or result.response_code != "DECIDED"
            or result.response_timestamp is None
            or result.response_timestamp <= 0
        ):
            return work["state"]
        try:
            decision = ADMISSION_DECISION.validate_python(result.response_data)
        except ValueError:
            return work["state"]
        target = {}
        if decision.result == "WORK_REQUIRED":
            task = await self.repository.task(session, work["task_id"])
            if (
                not task
                or decision.task_id != work["task_id"]
                or task["work_line_id"] != work["work_line_id"]
            ):
                return work["state"]
            plan = None
            if task["business_type"] == "outbound":
                plan = await self.manual_racks.original_target_plan(session, task)
                if not plan or task["plan_revision"] < 1 or task["status"] != "EXECUTING":
                    return work["state"]
            if plan and plan["payload"].get("target_rack"):
                proof = await self.manual_racks.target_permission(session, task, plan)
                if proof is None:
                    return work["state"]
                target = dict(target_job_id=proof[0], target_action_id=proof[1])
        wait_until = now_ms + decision.retry_after_ms if decision.result == "WAIT" else None
        await self._save(
            session,
            "request",
            admissions.c.operation_id == original["operation_id"],
            response=dict(
                code=result.response_code,
                timestamp=result.response_timestamp,
                data=result.response_data,
            ),
            saved_at_ms=now_ms,
            state="WAITING" if decision.result == "WAIT" else "CONSUMED",
            decision_code=decision.result,
            due_at_ms=wait_until,
        )
        state = decision.result
        command_code = None
        if decision.result == "NO_WORK":
            command_code = await self.roller.release_scan2(session, arrival_id=arrival_id)
            state = "RELEASED"
        await self._save(
            session,
            "manual_bin",
            table.c.arrival_id == arrival_id,
            state=state,
            pending=state == "WAIT",
            **target,
            command_code=command_code,
        )
        return state

    async def accept_manual_completed(
        self, ctx: CallbackContext, payload: ManualCompleted
    ) -> Outcome:
        if payload.completed_at > ctx.timestamp:
            raise Rejected("INVALID_DATA")
        _, task, _ = await self.repository.original_dispatch_task(
            ctx.session, self.roller, payload.task_id
        )
        if task is None:
            raise Unavailable()
        table = self.tables["input"]
        if await self._one(ctx.session, "input", table.c.operation_id == ctx.operation_id) is None:
            await ctx.session.execute(
                table.insert().values(
                    operation=ctx.operation,
                    operation_id=ctx.operation_id,
                    receipt_id=ctx.receipt_id,
                    work_line_id=task["work_line_id"],
                    timestamp=ctx.timestamp,
                    payload=payload.model_dump(mode="json"),
                    task_id=payload.task_id,
                    bin_code=payload.bin_code,
                    completion_result=payload.result,
                    completed_at=payload.completed_at,
                    state="PENDING",
                    arrival_id=None,
                    reason=None,
                    original_operation_id=None,
                )
            )
            await self.repository.note_received_work(
                ctx.session, task, operation=ctx.operation, record_id=ctx.operation_id
            )
        return Accepted()

    async def advance_completion(
        self, session: AsyncSession, *, operation_id: str, locked: bool = False
    ) -> str:
        table = self.tables["input"]
        located = await self._one(session, "input", table.c.operation_id == operation_id)
        if located is None:
            return "NOT_FOUND"
        if not locked:
            await self.repository.lock_dispatch_line(
                session, self.roller, located["work_line_id"], task_id=located["task_id"]
            )
        notice = await self._one(session, "input", table.c.operation_id == operation_id, lock=True)
        if notice["state"] != "PENDING":
            return notice["state"]
        prior = (
            (
                await session.execute(
                    select(table)
                    .where(
                        table.c.operation == BIN_COMPLETION_OPERATION,
                        table.c.operation_id != operation_id,
                        table.c.state != "PENDING",
                        *(
                            getattr(table.c, key) == notice[key]
                            for key in ("task_id", "bin_code", "completion_result", "completed_at")
                        ),
                    )
                    .order_by(table.c.operation_id)
                )
            )
            .mappings()
            .first()
        )
        if prior is not None:
            state = "REVIEW" if prior["state"] == "REVIEW" else "DUPLICATE"
            await self._save(
                session,
                "input",
                table.c.operation_id == operation_id,
                state=state,
                arrival_id=prior["arrival_id"],
                reason=prior["reason"],
                original_operation_id=prior["original_operation_id"] or prior["operation_id"],
            )
            return state
        task = await self.repository.task(session, notice["task_id"])
        if task and task["status"] in ("COMPLETED", "CANCELLED"):
            await self._save(
                session,
                "input",
                table.c.operation_id == operation_id,
                state="APPLIED",
                reason="TASK_CLOSED",
            )
            return "APPLIED"
        works = self.tables["manual_bin"]
        candidates = (
            (
                await session.execute(
                    select(works)
                    .where(
                        works.c.work_line_id == notice["work_line_id"],
                        works.c.bin_code == notice["bin_code"],
                        works.c.state == "WORK_REQUIRED",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        reason = "AMBIGUOUS" if len(candidates) > 1 else "NO_MATCH"
        if not candidates:
            completed = (
                (
                    await session.execute(
                        select(works).where(
                            works.c.work_line_id == notice["work_line_id"],
                            works.c.task_id == notice["task_id"],
                            works.c.bin_code == notice["bin_code"],
                            works.c.completed_operation_id.is_not(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if len(completed) == 1:
                original = completed[0]
                same_result = original["completed_result"] == notice["completion_result"]
                state = "DUPLICATE" if same_result else "REVIEW"
                await self._save(
                    session,
                    "input",
                    table.c.operation_id == operation_id,
                    state=state,
                    arrival_id=original["arrival_id"],
                    reason=None if same_result else "RESULT_CONFLICT",
                    original_operation_id=original["completed_operation_id"],
                )
                return state
        if len(candidates) == 1:
            work = candidates[0]
            if work["task_id"] != notice["task_id"]:
                reason = "TASK_MISMATCH"
            else:
                command = await self.roller.release_scan2(session, arrival_id=work["arrival_id"])
                await self._save(
                    session,
                    "manual_bin",
                    works.c.arrival_id == work["arrival_id"],
                    state="RELEASED",
                    pending=False,
                    completed_operation_id=operation_id,
                    completed_result=notice["completion_result"],
                    command_code=command,
                )
                await self._save(
                    session,
                    "input",
                    table.c.operation_id == operation_id,
                    state="APPLIED",
                    arrival_id=work["arrival_id"],
                )
                return "APPLIED"
        await self._save(
            session,
            "input",
            table.c.operation_id == operation_id,
            state="REVIEW",
            reason=reason,
        )
        return "REVIEW"
