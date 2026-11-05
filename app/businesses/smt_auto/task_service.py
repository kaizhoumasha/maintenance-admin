"""Transactional S1 task and plan operations for the AUTO host."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
from app.businesses._components.bin_roller.service import BinRoller
from app.businesses._components.sorting_task.contracts import PrepareAccepted, validate_task_branch
from app.businesses._components.sorting_task.ports import TaskOwnerRef
from app.businesses._components.sorting_task.request.sorting_task_prepare_v1 import Prepare
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.wms_callback import Accepted, Conflict, Duplicate, Rejected, Unavailable
from app.models import WorkLine
from app.reliable import ScopeRef

from .archive import archive_guard
from .models import (
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from .queue import Cancellation


def _digest(payload) -> str:
    data = payload.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AutoTaskService:
    """Owns task identity, contiguous plan versions, queue changes and cancellation."""

    def __init__(self, *, wms=None, roller: BinRoller | None = None, first_plan_wait_ms=None):
        if first_plan_wait_ms is not None and (
            type(first_plan_wait_ms) is not int or first_plan_wait_ms <= 0
        ):
            raise ValueError("first plan wait must be positive integer milliseconds")
        self.first_plan_wait_ms = first_plan_wait_ms
        self.wms = wms
        # The unassembled S1 foundation does not deliver supply; S6 must inject its roller.
        self.roller = roller

    async def archive_guard(self, ctx, payload):
        return await archive_guard(ctx, payload)

    async def read_task_owner(self, session, *, task_id):
        task = await session.get(AutoTask, task_id, populate_existing=True)
        return (
            None
            if task is None
            else TaskOwnerRef(
                task.task_id,
                "smt_auto",
                task.task_type,
                task.business_type,
                task.work_line_id,
                task.workline_code,
            )
        )

    async def prepare_next(self, session, *, work_line_id, now_ms):
        """Caller owns transaction/isolation; the line lock serializes its execution slot."""
        line = await session.get(
            WorkLine, work_line_id, with_for_update=True, populate_existing=True
        )
        if line is None or not line.is_enabled or line.business_code != "smt_auto":
            return None
        if await session.scalar(
            select(AutoTask.task_id).where(
                AutoTask.work_line_id == work_line_id,
                AutoTask.status.in_(("PREPARING", "EXECUTING")),
            )
        ):
            return None
        task = await session.scalar(
            select(AutoTask)
            .where(
                AutoTask.work_line_id == work_line_id,
                AutoTask.workline_code == line.line_code,
                AutoTask.status == "QUEUED",
                or_(AutoTask.not_before_ms.is_(None), AutoTask.not_before_ms <= now_ms),
            )
            .order_by(AutoTask.dispatch_sequence, AutoTask.issued_timestamp, AutoTask.task_id)
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            return None
        operation_id = str(uuid7())
        payload = Prepare(task_id=task.task_id, workline_code=task.workline_code)
        message = await self.wms.send(
            session,
            payload,
            operation_id=operation_id,
            scope=ScopeRef("smt_auto", "TASK", task.task_id),
        )
        session.add(
            AutoTaskRequest(
                task_id=task.task_id,
                purpose="PREPARE",
                operation="sorting.task.prepare@v1",
                operation_id=operation_id,
                message_id=message.message_id,
                request_data=payload.model_dump(mode="json"),
            )
        )
        task.status = "PREPARING"
        await session.flush()
        return task.task_id

    async def _prepare(self, session, task):
        return await session.scalar(
            select(AutoTaskRequest)
            .where(
                AutoTaskRequest.task_id == task.task_id,
                AutoTaskRequest.purpose == "PREPARE",
                AutoTaskRequest.operation == "sorting.task.prepare@v1",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def advance_prepare(self, session, *, task_id, now_ms):
        from types import SimpleNamespace

        task = await self._task(SimpleNamespace(session=session), task_id)
        if task is None:
            return "NOT_FOUND"
        original = await self._prepare(session, task)
        if original is None:
            return task.status
        if original.request_data != {"task_id": task.task_id, "workline_code": task.workline_code}:
            return task.status
        if not original.unresolved:
            if (
                task.status == "CANCELLED"
                and task.prepare_accepted_at_ms is not None
                and original.consumed_at_ms is not None
            ):
                task.pending = False
            return task.status
        result = await self.wms.get_result(
            session,
            message_id=original.message_id,
            operation=original.operation,
            operation_id=original.operation_id,
        )
        if (
            (result.message_id, result.operation, result.operation_id)
            != (original.message_id, original.operation, original.operation_id)
            or result.availability != "FOUND"
            or result.response_state != "VALID"
        ):
            return task.status
        try:
            PrepareAccepted.parse(result.http_status, result.response_code, result.response_data)
        except ValueError:
            return task.status
        original.response_snapshot = {
            "http_status": result.http_status,
            "code": result.response_code,
            "data": result.response_data,
            "timestamp": result.response_timestamp,
        }
        original.response_recorded_at_ms = original.response_recorded_at_ms or now_ms
        original.consumed_at_ms = now_ms
        original.unresolved = False
        if task.prepare_accepted_at_ms is None:
            task.prepare_accepted_at_ms = now_ms
            task.first_plan_wait_ms = self.first_plan_wait_ms
            task.first_plan_deadline_ms = (
                None if self.first_plan_wait_ms is None else now_ms + self.first_plan_wait_ms
            )
        if task.status == "CANCELLED":
            task.pending = False
        return task.status

    async def recheck_task_plan(self, ctx, payload, original_response):
        original = await self._input(ctx, payload.task_id)
        if original is None or original.disposition != "BLOCKED":
            return None
        if original.business_outcome != {
            "code": original_response["code"],
            "data": original_response["data"],
        }:
            return None
        if (
            original.effective_digest != _digest(payload)
            or original.request_timestamp != ctx.timestamp
        ):
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        return await self.accept_task_plan_delta(ctx, payload)

    async def accept_task_issued(self, ctx, payload):
        session = ctx.session
        validate_task_branch(payload, task_type="AUTO", business_type=payload.business_type)
        line = await ctx.session.scalar(
            select(WorkLine)
            .where(WorkLine.line_code == payload.workline_code)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing = await session.get(
            AutoTask, payload.task_id, with_for_update=True, populate_existing=True
        )
        digest = _digest(payload)
        if existing is not None:
            prior = await session.scalar(
                select(AutoTaskInput)
                .execution_options(populate_existing=True)
                .where(
                    AutoTaskInput.task_id == payload.task_id,
                    AutoTaskInput.operation == ctx.operation,
                    AutoTaskInput.operation_id == ctx.operation_id,
                )
            )
            if (
                prior
                and prior.effective_digest == digest
                and prior.request_timestamp == ctx.timestamp
            ):
                return Duplicate({"task_id": payload.task_id})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        if line is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        if not line.is_enabled or line.business_code != "smt_auto":
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        task = AutoTask(
            task_id=payload.task_id,
            task_type=payload.task_type,
            business_type=payload.business_type,
            work_line_id=line.id,
            workline_code=payload.workline_code,
            issued_operation_id=ctx.operation_id,
            issued_timestamp=ctx.timestamp,
            issued_data=payload.model_dump(mode="json"),
            queue_revision=payload.queue_revision,
            dispatch_sequence=payload.dispatch_sequence,
            not_before_ms=payload.not_before,
            plan_revision=0,
        )
        session.add(task)
        session.add(
            AutoTaskInput(
                task=task,
                operation=ctx.operation,
                operation_id=ctx.operation_id,
                request_timestamp=ctx.timestamp,
                first_received_at_ms=ctx.timestamp,
                effective_digest=digest,
                contract_version="v1",
                effective_request=payload.model_dump(mode="json"),
                disposition="APPLIED",
                applied_queue_revision=payload.queue_revision,
            )
        )
        return Accepted({"task_id": payload.task_id, "queue_revision": payload.queue_revision})

    async def accept_task_queue_changed(self, ctx, payload):
        task = await self._task(ctx, payload.task_id)
        if task is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(payload, task_type=task.task_type, business_type=task.business_type)
        if task.queue_revision >= payload.queue_revision:
            prior = await self._input(ctx, payload.task_id)
            if (
                prior
                and prior.effective_digest == _digest(payload)
                and prior.request_timestamp == ctx.timestamp
            ):
                return Duplicate({"task_id": payload.task_id})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        if task.status != "QUEUED":
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        line = await ctx.session.get(WorkLine, task.work_line_id)
        if (
            line is None
            or not line.is_enabled
            or line.business_code != "smt_auto"
            or line.line_code != task.workline_code
        ):
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        if payload.queue_revision != task.queue_revision + 1:
            raise Conflict({"reason_code": "REVISION_CONFLICT"})
        task.queue_revision = payload.queue_revision
        if payload.dispatch_sequence is not None:
            task.dispatch_sequence = payload.dispatch_sequence
        if "not_before" in payload.model_fields_set:
            task.not_before_ms = payload.not_before
        session = ctx.session
        session.add(
            self._input_row(ctx, task, payload, applied_queue_revision=payload.queue_revision)
        )
        return Accepted({"task_id": task.task_id, "queue_revision": task.queue_revision})

    async def accept_task_plan_delta(self, ctx, payload):
        task = await self._task(ctx, payload.task_id)
        if task is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(payload, task_type=task.task_type, business_type=task.business_type)
        digest = _digest(payload)
        original = await self._input(ctx, payload.task_id)
        if original is not None:
            if original.effective_digest != digest or original.request_timestamp != ctx.timestamp:
                raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
            if original.disposition in {"APPLIED", "DUPLICATE"}:
                return Duplicate()
        if task.status not in {"PREPARING", "EXECUTING"}:
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        prepare = await self._prepare(ctx.session, task)
        if prepare is None or prepare.request_data != {
            "task_id": task.task_id,
            "workline_code": task.workline_code,
        }:
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        line = await ctx.session.get(
            WorkLine, task.work_line_id, with_for_update=True, populate_existing=True
        )
        if line is None or line.line_code != task.workline_code or line.business_code != "smt_auto":
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        reason, outcome = None, None
        if not line.is_enabled:
            reason, outcome = "WORKLINE_DISABLED", Conflict({"reason_code": "STATE_CONFLICT"})
        elif task.prepare_accepted_at_ms is None or prepare.unresolved:
            reason, outcome = "PREPARE_PENDING", Unavailable()
        elif payload.plan_revision > task.plan_revision + 1:
            reason, outcome = "MISSING_REVISION", Conflict({"reason_code": "REVISION_CONFLICT"})
        if outcome is not None:
            row = original or self._input_row(ctx, task, payload)
            row.disposition, row.block_reason = "BLOCKED", reason
            row.requested_revision = payload.plan_revision
            row.business_outcome = {
                "code": "UNAVAILABLE" if isinstance(outcome, Unavailable) else "CONFLICT",
                "data": getattr(outcome, "data", {}),
            }
            ctx.session.add(row)
            return outcome
        prior = await session_scalar(ctx, payload.task_id, payload.plan_revision)
        if prior is not None and payload.plan_revision == task.plan_revision:
            if prior.effective_digest == digest:
                row = original or self._input_row(ctx, task, payload)
                row.disposition, row.canonical_input_id = "DUPLICATE", prior.id
                row.block_reason, row.business_outcome = None, None
                ctx.session.add(row)
                return Duplicate()
        if payload.plan_revision != task.plan_revision + 1:
            raise Conflict({"reason_code": "REVISION_CONFLICT"})
        task.plan_revision = payload.plan_revision
        task.status = "EXECUTING"
        row = original or self._input_row(ctx, task, payload)
        row.disposition, row.applied_plan_revision = "APPLIED", payload.plan_revision
        row.block_reason, row.business_outcome = None, None
        ctx.session.add(row)
        await ctx.session.flush()
        for rack_index, rack in enumerate(payload.added_bin_source_racks or []):
            for face_index, face in enumerate(rack.rack_face):
                ctx.session.add(
                    AutoSupplyMember(
                        task_id=task.task_id,
                        plan_input_id=row.id,
                        plan_revision=payload.plan_revision,
                        rack_id=rack.rack_id,
                        rack_face=face,
                        rack_array_index=rack_index,
                        face_array_index=face_index,
                        supply_change_id=ctx.operation_id,
                    )
                )
        for rack_index, rack in enumerate(payload.added_source_racks or []):
            ctx.session.add(
                AutoInboundSourceMember(
                    task_id=task.task_id,
                    plan_input_id=row.id,
                    plan_revision=payload.plan_revision,
                    rack_id=rack.rack_id,
                    rack_array_index=rack_index,
                )
            )
        for rack_index, rack in enumerate(payload.added_return_racks or []):
            for face in rack.rack_faces:
                ctx.session.add(
                    AutoOutboundSourceMember(
                        task_id=task.task_id,
                        plan_input_id=row.id,
                        plan_revision=payload.plan_revision,
                        rack_id=rack.rack_id,
                        rack_face=face,
                        rack_array_index=rack_index,
                    )
                )
        await self._supply_change(
            ctx.session,
            task,
            row,
            added=tuple(
                SourceFaceRef(payload.plan_revision, rack.rack_id, face)
                for rack in payload.added_bin_source_racks or []
                for face in rack.rack_face
            ),
        )
        return Accepted()

    async def accept_task_cancel(self, ctx, payload):
        task = await self._task(ctx, payload.task_id)
        if task is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(payload, task_type=task.task_type, business_type=task.business_type)
        prior = await self._input(ctx, payload.task_id)
        if prior is not None:
            if (
                prior.effective_digest == _digest(payload)
                and prior.request_timestamp == ctx.timestamp
            ):
                return Duplicate({"task_id": task.task_id, "status": task.status})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        if payload.cancel_scope == "TASK":
            if task.status not in {"QUEUED", "PREPARING"} or task.plan_revision != 0:
                raise Conflict({"reason_code": "STATE_CONFLICT"})
            Cancellation(payload.task_id, ctx.parsed.get("cancel_reason", "WMS_CANCEL")).apply(
                task, closed_at_ms=unix_ms(datetime.now(UTC))
            )
            ctx.session.add(self._input_row(ctx, task, payload))
            # An admitted SCAN2 can still own work and release while the plan is at revision 0.
            return Accepted({"task_id": task.task_id, "status": task.status})
        if payload.cancel_scope != "PLAN_MEMBERS":
            raise Rejected("INVALID_DATA")
        members = []
        for selectors, model in (
            (payload.bin_source_racks, AutoSupplyMember),
            (payload.source_racks, AutoInboundSourceMember),
            (payload.direct_pick_sources, AutoOutboundSourceMember),
        ):
            for selector in selectors or []:
                faces = (
                    selector.rack_face
                    if model is AutoSupplyMember
                    else [selector.rack_face if model is AutoOutboundSourceMember else None]
                )
                for face in faces:
                    selected = await self._members(
                        ctx, task.task_id, selector.plan_revision, selector.rack_id, face, model
                    )
                    if not selected:
                        raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
                    members.extend(selected)
        row = self._input_row(ctx, task, payload)
        ctx.session.add(row)
        await ctx.session.flush()
        withdrawn = []
        for member in members:
            if (
                isinstance(member, AutoOutboundSourceMember)
                and member.first_work_plan_round_id is not None
            ):
                continue
            if member.withdrawn_by_input_id is None:
                if isinstance(member, AutoSupplyMember):
                    withdrawn.append(
                        SourceFaceRef(member.plan_revision, member.rack_id, member.rack_face)
                    )
                member.withdrawn_by_input_id = row.id
                member.withdrawn_at_ms = ctx.timestamp
        await self._supply_change(ctx.session, task, row, withdrawn=tuple(withdrawn))
        return Accepted(
            {
                "task_id": task.task_id,
                "withdrawn_member_ids": [
                    m.id for m in members if m.withdrawn_by_input_id == row.id
                ],
            }
        )

    async def _supply_change(self, session, task, row, *, added=(), withdrawn=()):
        if not added and not withdrawn:
            return
        change = SupplyChange(task.work_line_id, task.task_id, row.operation_id, added, withdrawn)
        row.supply_change_id = change.change_id
        row.supply_change = {
            "added": [asdict(source) for source in added],
            "withdrawn": [asdict(source) for source in withdrawn],
        }
        await session.flush()
        if self.roller is not None:
            await self.roller.apply_supply_change(session, change)

    async def _members(
        self, ctx, task_id, plan_revision, rack_id, rack_face, model=AutoSupplyMember
    ):
        return tuple(
            await ctx.session.scalars(
                select(model)
                .where(
                    model.task_id == task_id,
                    model.plan_revision == plan_revision,
                    model.rack_id == rack_id,
                    *(
                        (model.rack_face == rack_face,)
                        if rack_face is not None and hasattr(model, "rack_face")
                        else ()
                    ),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )

    async def _task(self, ctx, task_id):
        line_id = await ctx.session.scalar(
            select(AutoTask.work_line_id).where(AutoTask.task_id == task_id)
        )
        if line_id is None:
            return None
        await ctx.session.get(WorkLine, line_id, with_for_update=True, populate_existing=True)
        return await ctx.session.get(
            AutoTask, task_id, with_for_update=True, populate_existing=True
        )

    async def _input(self, ctx, task_id):
        return await ctx.session.scalar(
            select(AutoTaskInput)
            .execution_options(populate_existing=True)
            .where(
                AutoTaskInput.task_id == task_id,
                AutoTaskInput.operation == ctx.operation,
                AutoTaskInput.operation_id == ctx.operation_id,
            )
        )

    def _input_row(self, ctx, task, payload, **versions):
        return AutoTaskInput(
            task=task,
            operation=ctx.operation,
            operation_id=ctx.operation_id,
            request_timestamp=ctx.timestamp,
            first_received_at_ms=ctx.timestamp,
            effective_digest=_digest(payload),
            contract_version="v1",
            effective_request=payload.model_dump(mode="json"),
            disposition="APPLIED",
            **versions,
        )


async def session_scalar(ctx, task_id, revision):
    return await ctx.session.scalar(
        select(AutoTaskInput)
        .execution_options(populate_existing=True)
        .where(
            AutoTaskInput.task_id == task_id,
            AutoTaskInput.applied_plan_revision == revision,
        )
    )
