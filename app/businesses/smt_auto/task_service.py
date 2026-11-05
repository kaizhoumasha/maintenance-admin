"""Transactional S1 task and plan operations for the AUTO host."""

import hashlib
import json

from sqlalchemy import select

from app.businesses._components.sorting_task.contracts import validate_task_branch
from app.integrations.wms_callback import Accepted, Conflict, Duplicate, Rejected
from app.models import WorkLine

from .models import (
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
)
from .queue import Cancellation


def _digest(payload) -> str:
    data = payload.model_dump(mode="json", exclude_none=True)
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AutoTaskService:
    """Owns task identity, contiguous plan versions, queue changes and cancellation."""

    async def accept_task_issued(self, ctx, payload):
        session = ctx.session
        existing = await session.get(AutoTask, payload.task_id, with_for_update=True)
        digest = _digest(payload)
        if existing is not None:
            prior = await session.scalar(
                select(AutoTaskInput).where(
                    AutoTaskInput.task_id == payload.task_id,
                    AutoTaskInput.operation == ctx.operation,
                    AutoTaskInput.operation_id == ctx.operation_id,
                )
            )
            if prior and prior.effective_digest == digest:
                return Duplicate({"task_id": payload.task_id})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        line = await ctx.session.scalar(
            select(WorkLine).where(WorkLine.line_code == payload.workline_code).with_for_update()
        )
        if line is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        if not line.is_enabled or line.business_code != "smt_auto" or payload.task_type != "AUTO":
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
            if prior and prior.effective_digest == _digest(payload):
                return Duplicate({"task_id": payload.task_id})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
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
        prior = await session_scalar(ctx, payload.task_id, payload.plan_revision)
        if prior is not None:
            if prior.effective_digest == digest:
                return Duplicate({"task_id": task.task_id, "plan_revision": payload.plan_revision})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        if payload.plan_revision != task.plan_revision + 1:
            raise Conflict({"reason_code": "MISSING_REVISION"})
        task.plan_revision = payload.plan_revision
        row = self._input_row(ctx, task, payload, applied_plan_revision=payload.plan_revision)
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
        return Accepted({"task_id": task.task_id, "plan_revision": task.plan_revision})

    async def accept_task_cancel(self, ctx, payload):
        task = await self._task(ctx, payload.task_id)
        if task is None:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        validate_task_branch(payload, task_type=task.task_type, business_type=task.business_type)
        prior = await self._input(ctx, payload.task_id)
        if prior is not None:
            if prior.effective_digest == _digest(payload):
                return Duplicate({"task_id": task.task_id, "status": task.status})
            raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
        if payload.cancel_scope == "TASK":
            Cancellation(payload.task_id, ctx.parsed.get("cancel_reason", "WMS_CANCEL")).apply(
                task, closed_at_ms=ctx.timestamp
            )
            ctx.session.add(self._input_row(ctx, task, payload))
            return Accepted({"task_id": task.task_id, "status": task.status})
        if payload.cancel_scope != "PLAN_MEMBERS":
            raise Rejected("INVALID_DATA")
        selectors = [
            bool(payload.bin_source_racks),
            bool(payload.source_racks),
            bool(payload.direct_pick_sources),
        ]
        if sum(selectors) != 1:
            raise Rejected("INVALID_DATA")
        if payload.bin_source_racks and task.business_type != "inbound":
            raise Rejected("INVALID_DATA")
        if payload.source_racks and task.business_type != "inbound":
            raise Rejected("INVALID_DATA")
        if payload.direct_pick_sources and task.business_type != "outbound":
            raise Rejected("INVALID_DATA")
        if payload.bin_source_racks:
            members = [
                member
                for selector in payload.bin_source_racks or []
                for face in selector.rack_face
                for member in (
                    await self._members(
                        ctx, task.task_id, selector.plan_revision, selector.rack_id, face
                    )
                )
            ]
        elif payload.source_racks:
            members = [
                member
                for selector in payload.source_racks
                for member in await self._members(
                    ctx,
                    task.task_id,
                    selector.plan_revision,
                    selector.rack_id,
                    None,
                    AutoInboundSourceMember,
                )
            ]
        else:
            members = [
                member
                for selector in payload.direct_pick_sources
                for member in await self._members(
                    ctx,
                    task.task_id,
                    selector.plan_revision,
                    selector.rack_id,
                    selector.rack_face,
                    AutoOutboundSourceMember,
                )
            ]
        if not members or any(member.withdrawn_by_input_id is not None for member in members):
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        row = self._input_row(ctx, task, payload)
        ctx.session.add(row)
        await ctx.session.flush()
        for member in members:
            member.withdrawn_by_input_id = row.id
            member.withdrawn_at_ms = ctx.timestamp
        return Accepted({"task_id": task.task_id, "withdrawn_member_ids": [m.id for m in members]})

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
            )
        )

    async def _task(self, ctx, task_id):
        return await ctx.session.get(AutoTask, task_id, with_for_update=True)

    async def _input(self, ctx, task_id):
        return await ctx.session.scalar(
            select(AutoTaskInput).where(
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
        select(AutoTaskInput).where(
            AutoTaskInput.task_id == task_id,
            AutoTaskInput.applied_plan_revision == revision,
        )
    )
