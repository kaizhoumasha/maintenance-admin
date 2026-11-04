"""Task-only production flow; all writes use the caller's original transaction."""

from collections.abc import Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
from app.businesses._components.bin_roller.service import BinRoller
from app.businesses._components.sorting_task.contracts import CompletionDecision
from app.businesses._components.sorting_task.ports import TaskOwnerRef
from app.businesses._components.sorting_task.request.sorting_task_completion_confirm_v1 import (
    CompletionConfirm,
)
from app.businesses._components.sorting_task.request.sorting_task_prepare_v1 import Prepare
from app.businesses.runtime import Capabilities
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.wms_callback import (
    Accepted,
    CallbackContext,
    Conflict,
    Duplicate,
    Outcome,
    Unavailable,
)
from app.models import WorkLine
from app.reliable import ScopeRef

from .contracts import (
    Cancel,
    Issued,
    PlanDelta,
    PrepareAccepted,
    QueueChanged,
    validate_task_branch,
)
from .manual_rack_flow import ManualRackFlow
from .models import BIN_COMPLETION_OPERATION, RACK_COMPLETION_OPERATIONS
from .repository import TaskRepository


class SmtManualTaskFlow:
    def __init__(
        self,
        capabilities: Capabilities,
        roller: BinRoller,
        *,
        repository: TaskRepository | None = None,
        first_plan_wait_ms: int | None = None,
    ) -> None:
        if first_plan_wait_ms is not None and (
            type(first_plan_wait_ms) is not int or first_plan_wait_ms <= 0
        ):
            raise ValueError("first_plan_wait_ms must be a positive integer or None")
        self.first_plan_wait_ms = first_plan_wait_ms
        self.capabilities, self.roller = capabilities, roller
        self.repository = repository or TaskRepository()
        self.manual_racks = ManualRackFlow(capabilities, roller, repository=self.repository)

    async def read_task_owner(self, session: AsyncSession, *, task_id: str) -> TaskOwnerRef | None:
        task = await self.repository.task(session, task_id)
        return (
            None
            if task is None
            else TaskOwnerRef(
                task_id=task["task_id"],
                business_code="smt_manual",
                task_type=task["task_type"],
                business_type=task["business_type"],
                work_line_id=task["work_line_id"],
                workline_code=task["workline_code"],
            )
        )

    async def accept_task_issued(self, ctx: CallbackContext, payload: Issued) -> Outcome:
        existing = await self.repository.task(ctx.session, payload.task_id)
        if existing is not None:
            return Conflict({"reason_code": "STATE_CONFLICT"})
        line = await self.repository.line_by_code(ctx.session, payload.workline_code)
        if line is None:
            return Conflict({"reason_code": "REFERENCE_CONFLICT"})
        if (
            not line.is_enabled
            or line.business_code != "smt_manual"
            or payload.task_type != "MANUAL"
        ):
            return Conflict({"reason_code": "STATE_CONFLICT"})
        if await self.repository.task(ctx.session, payload.task_id, lock=True) is not None:
            return Conflict({"reason_code": "STATE_CONFLICT"})
        table = self.repository.tables["task"]
        insert = pg_insert if ctx.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        created = await ctx.session.scalar(
            insert(table)
            .values(
                **payload.model_dump(mode="json"),
                work_line_id=line.id,
                issued_operation_id=ctx.operation_id,
                issued_timestamp=ctx.timestamp,
                issued_data=payload.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(index_elements=[table.c.task_id])
            .returning(table.c.task_id)
        )
        if created is None:
            return Conflict({"reason_code": "STATE_CONFLICT"})
        await self.repository.save_input(ctx, payload, applied=True)
        return Accepted()

    async def accept_task_queue_changed(
        self, ctx: CallbackContext, payload: QueueChanged
    ) -> Outcome:
        _, task = await self.repository.original(ctx.session, payload.task_id)
        if task is None:
            return Conflict({"reason_code": "REFERENCE_CONFLICT"})
        if payload.queue_revision != task["queue_revision"] + 1:
            return Conflict({"reason_code": "REVISION_CONFLICT"})
        values = payload.model_dump(mode="json", exclude={"task_id", "queue_revision"})
        if task["status"] != "QUEUED" or all(task[key] == value for key, value in values.items()):
            return Conflict({"reason_code": "STATE_CONFLICT"})
        await self.repository.save_task(
            ctx.session, payload.task_id, queue_revision=payload.queue_revision, **values
        )
        await self.repository.save_input(ctx, payload, applied=True)
        return Accepted()

    async def accept_task_plan_delta(self, ctx: CallbackContext, payload: PlanDelta) -> Outcome:
        line, task, jobs = await self.repository.original_dispatch_task(
            ctx.session, self.roller, payload.task_id
        )
        if task is None:
            return Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(
            payload, task_type=task["task_type"], business_type=task["business_type"]
        )
        if task["status"] not in {"PREPARING", "EXECUTING"}:
            await self.repository.save_input(ctx, payload, applied=False)
            return Conflict({"reason_code": "STATE_CONFLICT"})
        data = payload.model_dump(mode="json")
        plans = self.repository.tables["input"]
        if payload.plan_revision == task["plan_revision"]:
            original = await ctx.session.scalar(
                select(plans.c.payload).where(
                    plans.c.task_id == payload.task_id,
                    plans.c.plan_revision == payload.plan_revision,
                    plans.c.is_plan_record.is_(True),
                )
            )
            if original == data:
                await self.repository.save_input(ctx, payload, applied=True)
                return Duplicate()
            await self.repository.save_input(ctx, payload, applied=False)
            return Conflict({"reason_code": "REVISION_CONFLICT"})
        reason, outcome = None, None
        if not task["prepare_operation_id"]:
            outcome = Conflict({"reason_code": "STATE_CONFLICT"})
        elif (
            line is None
            or line.business_code != "smt_manual"
            or line.line_code != task["workline_code"]
        ):
            outcome = Conflict({"reason_code": "STATE_CONFLICT"})
        elif not line.is_enabled:
            reason, outcome = "WORKLINE_DISABLED", Conflict({"reason_code": "STATE_CONFLICT"})
        elif not task["prepare_accepted"]:
            reason, outcome = "PREPARE_PENDING", Unavailable()
        elif payload.plan_revision > task["plan_revision"] + 1:
            reason, outcome = "MISSING_REVISION", Conflict({"reason_code": "REVISION_CONFLICT"})
        elif payload.plan_revision < task["plan_revision"]:
            outcome = Conflict({"reason_code": "REVISION_CONFLICT"})
        elif (payload.target_rack and not line.position_bindings.get("TRANSFER_RACK")) or (
            payload.added_direct_picks and not line.position_bindings.get("RETURN_RACK")
        ):
            outcome = Conflict({"reason_code": "STATE_CONFLICT"})
        if outcome is not None:
            await self.repository.save_input(ctx, payload, applied=False, reason=reason)
            return outcome
        members = self.repository.tables["member"]
        added = tuple(
            SourceFaceRef(payload.plan_revision, rack.rack_id, face)
            for rack in payload.added_bin_source_racks or []
            for face in rack.rack_face
        )
        values = [
            dict(kind="BIN", rack_id=face.rack_id, rack_face=face.rack_face, slot_id="")
            for face in added
        ]
        values.extend(
            dict(
                kind="DIRECT",
                rack_id=pick.source_locator.rack_id,
                rack_face=pick.source_locator.rack_face,
                slot_id=pick.source_locator.slot_id,
            )
            for pick in payload.added_direct_picks or []
        )
        for index, member in enumerate(values):
            await ctx.session.execute(
                members.insert().values(
                    id=str(uuid7()),
                    task_id=payload.task_id,
                    plan_revision=payload.plan_revision,
                    member_order=index,
                    **member,
                )
            )
        await self.roller.apply_supply_change(
            ctx.session,
            SupplyChange(
                task["work_line_id"],
                payload.task_id,
                ctx.operation_id,
                added,
                (),
            ),
        )
        if added:
            await self.roller.rack_dispatch.note_facts_changed(
                ctx.session,
                job_ids=tuple(
                    j.id
                    for j in jobs
                    if j.origin.task_id == payload.task_id
                    and j.origin.rack_id in {f.rack_id for f in added}
                ),
            )
        await self.repository.save_task(
            ctx.session,
            payload.task_id,
            plan_revision=payload.plan_revision,
            status="EXECUTING",
            **(
                {"target_rack": payload.target_rack.model_dump(mode="json")}
                if payload.target_rack
                else {}
            ),
        )
        await self.repository.save_input(ctx, payload, applied=True)
        await ctx.session.execute(
            plans.update()
            .where(plans.c.operation == ctx.operation, plans.c.operation_id == ctx.operation_id)
            .values(is_plan_record=True)
        )
        await self._register_task_rack_candidates(ctx.session, task_id=payload.task_id)
        return Accepted()

    async def _register_task_rack_candidates(self, session: AsyncSession, *, task_id: str) -> None:
        from app.businesses._components.bin_roller.rack_flow import select_supply_cycle

        task = await self.repository.task(session, task_id)
        if not task or task["status"] != "EXECUTING" or not task["prepare_accepted"]:
            return
        current = await self.roller.production.read_current_task(
            session, work_line_id=task["work_line_id"]
        )
        if (
            not current.is_valid
            or current.task_id != task_id
            or await self.repository.has_pending_plan(session, task_id)
        ):
            return
        await self.manual_racks.reconcile_task(
            session, task_id=task_id, register_only=True, roles=("TRANSFER",)
        )
        repo = self.roller.repository(task["work_line_id"])
        faces = repo.tables["face_work"]
        members = self.repository.tables["member"]
        ordered = (
            (
                await session.execute(
                    select(members)
                    .where(members.c.task_id == task_id, members.c.status == "PENDING")
                    .order_by(members.c.plan_revision, members.c.member_order)
                )
            )
            .mappings()
            .all()
        )
        supply_faces = (
            (
                await session.execute(
                    select(faces).where(
                        *repo.scoped(faces),
                        faces.c.purpose == "SUPPLY",
                        faces.c.scope_id == task_id,
                        faces.c.demand_state == "ACTIVE",
                    )
                )
            )
            .mappings()
            .all()
        )
        by_source = {
            (face["plan_revision"], face["rack_id"], face["rack_face"]): face
            for face in supply_faces
        }
        for revision in sorted({m["plan_revision"] for m in ordered}):
            for member in ordered:
                if member["plan_revision"] != revision or member["kind"] != "BIN":
                    continue
                face = by_source.get((revision, member["rack_id"], member["rack_face"]))
                if face and face["rack_job_id"] is None:
                    cycle = await select_supply_cycle(self.roller, session, face["id"])
                    if cycle:
                        await repo.bind_supply(session, demand_id=face["id"], cycle_id=cycle)
            await self.manual_racks.reconcile_task(
                session,
                task_id=task_id,
                register_only=True,
                roles=("RETURN",),
                through_revision=revision,
            )

    async def recheck_task_plan(
        self, ctx: CallbackContext, payload: PlanDelta, original_response: Mapping
    ) -> Outcome | None:
        original = await self.repository.input(ctx.session, ctx.operation, ctx.operation_id)
        if original is None or original["state"] == "APPLIED":
            return None
        reasons = {
            "PREPARE_PENDING": ("UNAVAILABLE", {}),
            "MISSING_REVISION": ("CONFLICT", {"reason_code": "REVISION_CONFLICT"}),
            "WORKLINE_DISABLED": ("CONFLICT", {"reason_code": "STATE_CONFLICT"}),
        }
        expected = reasons.get(original["reason"])
        if expected is None or (original_response["code"], original_response["data"]) != expected:
            return None
        return await self.accept_task_plan_delta(ctx, payload)

    async def accept_task_cancel(self, ctx: CallbackContext, payload: Cancel) -> Outcome:
        _, task, jobs = await self.repository.original_dispatch_task(
            ctx.session, self.roller, payload.task_id
        )
        if task is None:
            return Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(
            payload, task_type=task["task_type"], business_type=task["business_type"]
        )
        if payload.cancel_scope == "TASK":
            if task["status"] not in {"QUEUED", "PREPARING"} or task["plan_revision"] != 0:
                return Conflict({"reason_code": "STATE_CONFLICT"})
            await self.repository.save_task(ctx.session, payload.task_id, status="CANCELLED")
            await self.repository.stop_bin_scheduling(ctx.session, payload.task_id)
            await self.roller.close_scope(
                ctx.session,
                ScopeRef(self.roller.context.domain, "TASK", payload.task_id),
                reason="TASK_CANCELLED",
            )
        else:
            table = self.repository.tables["member"]
            rows = (
                (
                    await ctx.session.execute(
                        select(table)
                        .where(
                            table.c.task_id == payload.task_id,
                            table.c.status == "PENDING",
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            bins = {
                (item.plan_revision, item.rack_id, face)
                for item in payload.bin_source_racks or []
                for face in item.rack_face
            }
            selected, withdrawn = [], []
            for row in rows:
                key = (row["plan_revision"], row["rack_id"], row["rack_face"])
                matches = row["kind"] == "BIN" and key in bins
                if row["kind"] == "DIRECT":
                    matches = any(
                        key == (item.plan_revision, item.rack_id, item.rack_face)
                        and (item.slot_ids is None or row["slot_id"] in item.slot_ids)
                        for item in payload.direct_pick_sources or []
                    )
                if matches:
                    selected.append(row["id"])
                    if row["kind"] == "BIN":
                        withdrawn.append(SourceFaceRef(*key))
            if selected:
                await ctx.session.execute(
                    table.update()
                    .where(table.c.id.in_(selected))
                    .values(
                        status="CANCELLED",
                        cancelled_operation_id=ctx.operation_id,
                    )
                )
            await self.roller.apply_supply_change(
                ctx.session,
                SupplyChange(
                    task["work_line_id"],
                    payload.task_id,
                    ctx.operation_id,
                    (),
                    tuple(withdrawn),
                ),
            )
            if withdrawn:
                await self.roller.rack_dispatch.note_facts_changed(
                    ctx.session,
                    job_ids=tuple(
                        j.id
                        for j in jobs
                        if j.origin.task_id == payload.task_id
                        and j.origin.rack_id in {f.rack_id for f in withdrawn}
                    ),
                )
        await self.repository.save_input(ctx, payload, applied=True)
        return Accepted()

    async def prepare_next(
        self, session: AsyncSession, *, work_line_id: int, now_ms: int, locked: bool = False
    ) -> str | None:
        line = (
            await session.get(WorkLine, work_line_id)
            if locked
            else await self.repository.lock_line(session, work_line_id)
        )
        if line is None or not line.is_enabled or line.business_code != "smt_manual":
            return None
        table = self.repository.tables["task"]
        if await session.scalar(
            select(table.c.task_id).where(
                table.c.work_line_id == work_line_id,
                table.c.status.in_(("PREPARING", "EXECUTING")),
            )
        ):
            return None
        row = (
            (
                await session.execute(
                    select(table)
                    .where(
                        table.c.work_line_id == work_line_id,
                        table.c.status == "QUEUED",
                        or_(table.c.not_before.is_(None), table.c.not_before <= now_ms),
                    )
                    .order_by(table.c.dispatch_sequence, table.c.issued_timestamp, table.c.task_id)
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        operation_id = str(uuid7())
        message = await self.capabilities.wms.send(
            session,
            Prepare(task_id=row["task_id"], workline_code=row["workline_code"]),
            operation_id=operation_id,
            scope=ScopeRef(self.roller.context.domain, "TASK", row["task_id"]),
        )
        await session.execute(
            self.repository.tables["request"]
            .insert()
            .values(
                kind="PREPARE",
                operation="sorting.task.prepare@v1",
                operation_id=operation_id,
                task_id=row["task_id"],
                work_line_id=row["work_line_id"],
                message_id=message.message_id,
                request_snapshot=dict(task_id=row["task_id"], workline_code=row["workline_code"]),
            )
        )
        await self.repository.save_task(
            session,
            row["task_id"],
            status="PREPARING",
            prepare_operation_id=operation_id,
            prepare_message_id=message.message_id,
        )
        return row["task_id"]

    async def advance_task(
        self, session: AsyncSession, *, task_id: str, locked: bool = False
    ) -> str:
        """Consume original prepare/confirmation in the caller's independent transaction."""
        if not locked:
            await self.repository.original_dispatch_task(session, self.roller, task_id)
        task = await self.repository.task(session, task_id)
        if task is None:
            return "NOT_FOUND"
        if task["status"] not in {"PREPARING", "EXECUTING"}:
            return task["status"]
        now_ms = unix_ms(self.capabilities.wms.clock())
        if not task["prepare_accepted"]:
            if not task["prepare_message_id"]:
                return task["status"]
            requests = self.repository.tables["request"]
            original_prepare = (
                (
                    await session.execute(
                        select(requests).where(
                            requests.c.kind == "PREPARE",
                            requests.c.operation_id == task["prepare_operation_id"],
                            requests.c.task_id == task_id,
                            requests.c.work_line_id == task["work_line_id"],
                            requests.c.message_id == task["prepare_message_id"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if original_prepare is None:
                return task["status"]
            result = await self.capabilities.wms.get_result(
                session,
                message_id=original_prepare["message_id"],
                operation="sorting.task.prepare@v1",
                operation_id=task["prepare_operation_id"],
            )
            if result.availability != "FOUND" or result.response_state != "VALID":
                return task["status"]
            try:
                PrepareAccepted.parse(
                    result.http_status, result.response_code, result.response_data
                )
            except ValueError:
                return task["status"]
            await self.repository.save_task(
                session,
                task_id,
                prepare_accepted=True,
                prepare_response_timestamp=result.response_timestamp,
                prepare_accepted_at_ms=now_ms,
            )
            requests = self.repository.tables["request"]
            await session.execute(
                requests.update()
                .where(
                    requests.c.kind == "PREPARE",
                    requests.c.operation_id == task["prepare_operation_id"],
                    requests.c.task_id == task_id,
                    requests.c.work_line_id == task["work_line_id"],
                )
                .values(
                    state="CONSUMED",
                    decision_code="ACCEPTED",
                    saved_at_ms=now_ms,
                    response=dict(
                        code=result.response_code,
                        timestamp=result.response_timestamp,
                        data=result.response_data,
                    ),
                )
            )
            task = await self.repository.task(session, task_id)
        confirmation = await self.repository.confirmation(session, task)
        if task["confirmation_operation_id"] is not None and confirmation is None:
            return task["status"]
        ready, basis = await self._completion_basis(session, task, now_ms)
        table = self.repository.tables["request"]
        if confirmation is not None and confirmation["state"] == "REQUESTED":
            response = confirmation["response"]
            http_status = 200
            if response is None:
                result = await self.capabilities.wms.get_result(
                    session,
                    message_id=confirmation["message_id"],
                    operation="sorting.task.completion_confirm@v1",
                    operation_id=confirmation["operation_id"],
                )
                if (
                    result.availability != "FOUND"
                    or result.response_state != "VALID"
                    or result.response_timestamp is None
                ):
                    return task["status"]
                http_status = result.http_status
                response = dict(
                    code=result.response_code,
                    timestamp=result.response_timestamp,
                    data=result.response_data,
                )
            try:
                decision = CompletionDecision.parse(http_status, response["code"], response["data"])
                if (
                    decision.result == "PLAN_REVISION_STALE"
                    and decision.current_plan_revision <= confirmation["plan_revision"]
                ):
                    raise ValueError("STALE must exceed the original requested revision")
            except ValueError:
                return task["status"]
            state = decision.result
            notices = self.repository.tables["input"]
            if state == "COMPLETED" and await session.scalar(
                select(notices.c.operation_id)
                .where(
                    notices.c.task_id == task_id,
                    notices.c.operation == BIN_COMPLETION_OPERATION,
                    notices.c.state == "PENDING",
                )
                .limit(1)
            ):
                if confirmation["response"] is None:
                    await session.execute(
                        table.update()
                        .where(
                            table.c.kind == "COMPLETION_CONFIRM",
                            table.c.operation_id == confirmation["operation_id"],
                            table.c.task_id == task_id,
                        )
                        .values(response=response)
                    )
                return task["status"]
            if state == "COMPLETED" and (not ready or basis != confirmation["basis"]):
                state = "CONFLICT"
            await session.execute(
                table.update()
                .where(
                    table.c.kind == "COMPLETION_CONFIRM",
                    table.c.operation_id == confirmation["operation_id"],
                    table.c.task_id == task_id,
                )
                .values(
                    state="CONFLICT"
                    if state == "CONFLICT"
                    else "WAITING"
                    if state == "BUSINESS_IN_PROGRESS"
                    else "CONSUMED",
                    decision_code=decision.result,
                    saved_at_ms=now_ms,
                    response=response,
                    due_at_ms=now_ms + decision.retry_after_ms
                    if state == "BUSINESS_IN_PROGRESS"
                    else None,
                )
            )
            if state == "COMPLETED":
                await self.repository.save_task(session, task_id, status="COMPLETED")
                await self.repository.stop_bin_scheduling(session, task_id)
                await self.roller.close_scope(
                    session,
                    ScopeRef(self.roller.context.domain, "TASK", task_id),
                    reason="TASK_COMPLETED",
                )
                return "COMPLETED"
            confirmation = await self.repository.confirmation(session, task)
        if not ready:
            return task["status"]
        if confirmation is not None:
            if (
                confirmation["state"] in {"REQUESTED", "CONFLICT"}
                or confirmation["decision_code"] == "COMPLETED"
            ):
                return task["status"]
            if (
                confirmation["decision_code"] == "PLAN_REVISION_STALE"
                and task["plan_revision"]
                < confirmation["response"]["data"]["current_plan_revision"]
            ):
                return task["status"]
            if (
                confirmation["decision_code"] == "BUSINESS_IN_PROGRESS"
                and now_ms < confirmation["due_at_ms"]
                and basis == confirmation["basis"]
            ):
                return task["status"]
        operation_id = str(uuid7())
        message = await self.capabilities.wms.send(
            session,
            CompletionConfirm(task_id=task_id, last_applied_plan_revision=task["plan_revision"]),
            operation_id=operation_id,
            scope=ScopeRef(self.roller.context.domain, "TASK", task_id),
        )
        await session.execute(
            table.insert().values(
                kind="COMPLETION_CONFIRM",
                operation="sorting.task.completion_confirm@v1",
                operation_id=operation_id,
                task_id=task_id,
                work_line_id=task["work_line_id"],
                message_id=message.message_id,
                request_snapshot=dict(
                    task_id=task_id, last_applied_plan_revision=task["plan_revision"]
                ),
                previous_operation_id=task["confirmation_operation_id"],
                plan_revision=task["plan_revision"],
                basis=basis,
            )
        )
        await self.repository.save_task(session, task_id, confirmation_operation_id=operation_id)
        return task["status"]

    async def _completion_basis(
        self, session: AsyncSession, task: RowMapping, now_ms: int
    ) -> tuple[bool, dict]:
        tables = self.repository.tables
        if await self.repository.has_pending_plan(session, task["task_id"]):
            return False, {}
        if task["plan_revision"] == 0 and (
            self.first_plan_wait_ms is None
            or task["prepare_accepted_at_ms"] is None
            or now_ms < task["prepare_accepted_at_ms"] + self.first_plan_wait_ms
        ):
            return False, {}
        members = (
            (
                await session.execute(
                    select(tables["member"])
                    .where(tables["member"].c.task_id == task["task_id"])
                    .order_by(tables["member"].c.id)
                )
            )
            .mappings()
            .all()
        )
        progress = await self.roller.get_supply_progress(
            session, work_line_id=task["work_line_id"], task_id=task["task_id"]
        )
        active_sources = {
            SourceFaceRef(m["plan_revision"], m["rack_id"], m["rack_face"])
            for m in members
            if m["kind"] == "BIN" and m["status"] != "CANCELLED"
        }
        progress = tuple(p for p in progress if p.source in active_sources)
        if len(progress) != sum(
            m["kind"] == "BIN" and m["status"] != "CANCELLED" for m in members
        ):
            return False, {}
        for member in members:
            if member["status"] == "CANCELLED" and not member["cancelled_operation_id"]:
                return False, {}
            if member["status"] == "CANCELLED":
                continue
            if member["kind"] == "DIRECT":
                if member["status"] not in {"COMPLETED", "CANCELLED"}:
                    return False, {}
                continue
            matched = [
                p
                for p in progress
                if p.task_id == task["task_id"]
                and p.source
                == SourceFaceRef(member["plan_revision"], member["rack_id"], member["rack_face"])
            ]
            if len(matched) != 1 or not matched[0].original_ids or matched[0].pending:
                return False, {}
            if not matched[0].feed_complete and not (
                member["status"] == "CANCELLED" and member["cancelled_operation_id"]
            ):
                return False, {}
        # Never-admitted HOLD is scan evidence, not an accepted work obligation.
        bins = (
            (
                await session.execute(
                    select(tables["manual_bin"])
                    .where(
                        tables["manual_bin"].c.task_id == task["task_id"],
                        or_(
                            tables["manual_bin"].c.state != "HOLD",
                            tables["manual_bin"].c.admission_operation_id.is_not(None),
                        ),
                    )
                    .order_by(tables["manual_bin"].c.arrival_id)
                )
            )
            .mappings()
            .all()
        )
        if any(
            m["kind"] == "BIN" and m["status"] != "CANCELLED" for m in members
        ) and not bins:
            return False, {}
        faces = self.roller.repository(task["work_line_id"]).tables["face_work"]
        if await session.scalar(
            select(faces.c.id).where(
                faces.c.scope_id == task["task_id"],
                faces.c.purpose == "SUPPLY",
                faces.c.closed_at.is_(None),
                or_(
                    faces.c.current_transport_task_id.is_not(None),
                    faces.c.phase.in_(("FEED_MOVE", "RETURN_MOVE")),
                ),
            ).limit(1)
        ):
            return False, {}
        for work in bins:
            if not await self.repository.bin_release_evidence(session, self.roller, work):
                return False, {}
        inputs = tables["input"]
        if await session.scalar(
            select(inputs.c.operation_id)
            .where(
                inputs.c.task_id == task["task_id"],
                or_(
                    (inputs.c.operation == BIN_COMPLETION_OPERATION)
                    & inputs.c.state.in_(("PENDING", "REVIEW")),
                    inputs.c.operation.in_(RACK_COMPLETION_OPERATIONS)
                    & (inputs.c.state == "PENDING"),
                ),
            )
            .limit(1)
        ):
            return False, {}
        if await self.manual_racks.has_report_obligations(session, task_id=task["task_id"]):
            return False, {}
        if not await self._rack_completion_ready(session, task, members):
            return False, {}
        # Only business progress belongs here; tail release Results and duplicate receipts do not.
        return True, dict(
            plan_revision=task["plan_revision"],
            members=[[m["id"], m["status"], m["cancelled_operation_id"]] for m in members],
            arrivals=[
                [b["arrival_id"], b["command_code"], b["completed_operation_id"]] for b in bins
            ],
        )

    async def _rack_completion_ready(
        self, session: AsyncSession, task: RowMapping, members: Sequence[RowMapping]
    ) -> bool:
        from .manual_rack_flow import MANUAL_SOURCES

        jobs = await self.roller.rack_dispatch.jobs(
            session,
            work_line_id=task["work_line_id"],
            source_kinds=MANUAL_SOURCES,
            task_id=task["task_id"],
        )
        target_required = (
            task["business_type"] == "outbound"
            and task["target_rack"]
            and not any(m["kind"] == "BIN" for m in members)
        )
        target_ready = not target_required
        target = (
            await self.manual_racks.original_target_plan(session, task)
            if task["target_rack"]
            else None
        )
        for job in jobs:
            if job.review_reason in {"POSITION_CONFLICT", "OTHER_POSITION_CONFLICT"}:
                return False
            transfer = job.origin.source_kind == "MANUAL_TRANSFER"
            for action in job.actions:
                if action.kind not in ("ENTER", "ROTATE"):
                    continue
                result = await self.roller.rack_dispatch.final_result(
                    session, job_id=job.id, action_id=action.id
                )
                outcome = result.outcome if result else None
                if transfer and not target_required:
                    if action.kind == "ENTER" and outcome and outcome["status"] == "CANCELLED":
                        return False
                    continue
                if not (
                    result
                    and outcome
                    and not result.has_pending_outcome
                    and not outcome["position_unknown"]
                    and result.closure == "CLOSED"
                    and not (
                        result.position_observation
                        and result.position_observation["outcome_revision"]
                        > result.outcome_revision
                    )
                ):
                    return False
                if action.kind == "ENTER" and outcome["status"] == "CANCELLED":
                    associated = [m for m in members if m["rack_work_id"] == job.id]
                    if (
                        action.phase != "APPLIED"
                        or transfer
                        or not associated
                        or any(
                            m["status"] != "CANCELLED" or not m["cancelled_operation_id"]
                            for m in associated
                        )
                    ):
                        return False
                if (
                    transfer
                    and action.kind == "ENTER"
                    and target
                    and target["target_job_id"] == job.id
                    and target["target_action_id"] == action.id
                    and task["task_id"] == job.origin.source_ref
                ):
                    target_ready = (
                        outcome["status"] == "SUCCEEDED"
                        and action.phase == "APPLIED"
                        and outcome["final_position"]
                        == {"kind": "RACK_POSITION", "location_code": job.origin.point}
                        and outcome["arrival_face"] == task["target_rack"]["rack_face"]
                        and job.origin.rack_id == task["target_rack"]["rack_id"]
                    )
        return bool(target_ready)
