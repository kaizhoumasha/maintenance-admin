"""Inbound retention of original ownership and independent rack recovery evidence."""

from dataclasses import asdict

from sqlalchemy import delete, select, update

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    PointDemand,
)
from app.core.strict_json import canonical_digest
from app.integrations.wms_callback import CallbackResponse

from .locking import lock_line
from .models import (
    AutoBinWork,
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoInboundSourceMember,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)

RETENTION_MS = 7 * 24 * 60 * 60 * 1000
_TERMINAL = {"COMPLETED", "CANCELLED"}
_DETAILS = (
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoBinWork,
    AutoInboundSourceMember,
    AutoSupplyMember,
    AutoTaskRequest,
    AutoTaskInput,
)


async def archive_guard(ctx, payload):
    task = await ctx.session.get(AutoTask, payload.task_id, populate_existing=True)
    if (
        task is not None
        and task.business_type == "inbound"
        and task.status in _TERMINAL
        and task.details_cleaned
        and task.details_cleaned_at_ms is not None
    ):
        return CallbackResponse(
            409,
            {
                "operation_id": ctx.operation_id,
                "code": "CONFLICT",
                "timestamp": task.details_cleaned_at_ms,
                "data": {"reason_code": "STATE_CONFLICT"},
            },
            "conflict",
        )
    return None


def _fact(data):
    return FactRef(data["kind"], tuple(data["key"]), tuple(data["version"]))


def archived_facts(task, progress, point):
    """Restore coverage only for the exact independently recoverable public job."""
    if not task.details_cleaned or task.status not in _TERMINAL:
        return None
    try:
        saved = task.rack_retention_basis[progress.id]
        if (
            task.business_type != "inbound"
            or task.direct_obligations_closed_at_ms != saved["closed_at"]
            or task.details_cleaned_at_ms is None
            or saved["point"] != asdict(point)
            or saved["origin"] != canonical_digest(asdict(progress.origin))
            or saved["position_action_id"] != progress.position_action_id
            or saved["position"] != progress.actual_position
            or saved["face"] != progress.actual_face
            or not set(saved["action_ids"]) <= {a.id for a in progress.actions}
        ):
            return None
        scope = (
            point.owner_domain,
            str(point.work_line_id),
            point.job_id,
            point.rack_id,
            point.point,
        )
        coverage = tuple(
            Coverage(
                row["collection"],
                tuple(row["scope"]),
                _fact(row["anchor"]),
                tuple(_fact(fact) for fact in row["rows"]),
            )
            for row in saved["coverage"]
        )
        if not coverage or any(row.scope != scope for row in coverage):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return PointDemand(point, "NONE", coverage), DirectHandoff(point, "CLEAR", coverage)


def _departure_coverage(job, point):
    """An already begun departure retains the public, originally verified handoff."""
    if job.state != "RELEASING" or job.review_reason not in (None, "AWAITING_ORIGINAL_RESULT"):
        return None
    position = next((action for action in job.actions if action.id == job.position_action_id), None)
    if position is None or position.outcome_revision is None:
        return None
    for action in reversed(job.actions):
        if action.kind != "DEPARTURE_DECIDE":
            continue
        basis = action.basis or {}
        demand, handoff = basis.get("demand") or {}, basis.get("handoff") or {}
        if (
            demand.get("state") == "NONE"
            and handoff.get("state") == "CLEAR"
            and demand.get("point") == handoff.get("point") == asdict(point)
            and demand.get("coverage")
            and demand["coverage"] == handoff.get("coverage")
            and basis.get("position_action_id") == position.id
            and basis.get("outcome_revision") == position.outcome_revision
            and basis.get("position") == job.actual_position
            and basis.get("face") == job.actual_face
        ):
            return demand["coverage"]
        return None
    return None


async def _physical_deletion_order(session, task_id):
    plates = tuple(
        await session.scalars(select(AutoInboundPlate).where(AutoInboundPlate.task_id == task_id))
    )
    rounds = tuple(
        await session.scalars(
            select(AutoInboundDecisionRound).where(AutoInboundDecisionRound.task_id == task_id)
        )
    )
    dependencies = {}
    for plate in plates:
        dependencies[AutoInboundPlate, plate.id] = {
            node
            for node in (
                (AutoInboundPlate, plate.grant_parent_plate_id),
                (AutoInboundDecisionRound, plate.grant_round_id),
            )
            if node[1] is not None
        }
    for round_ in rounds:
        dependencies[AutoInboundDecisionRound, round_.id] = {
            node
            for node in (
                (AutoInboundPlate, round_.plate_id),
                (AutoInboundPlate, round_.trigger_plate_id),
                (AutoInboundDecisionRound, round_.previous_round_id),
            )
            if node[1] is not None
        }
    order = []
    while dependencies:
        referenced = set().union(*dependencies.values())
        if not referenced <= dependencies.keys():
            return None
        leaves = dependencies.keys() - referenced
        if not leaves:
            return None
        order.append(leaves)
        for node in leaves:
            del dependencies[node]
    return order


class Archive:
    def __init__(self, flow, racks):
        self.flow = flow
        self.racks = racks

    async def clean(self, session, *, task_id, now_ms):
        task = await session.get(AutoTask, task_id)
        if task is None or task.business_type != "inbound" or task.status not in _TERMINAL:
            return False
        dispatch = self.racks.dispatch
        await lock_line(session, dispatch, task.work_line_id)
        await session.refresh(task)
        if (
            task.business_type != "inbound"
            or task.status not in _TERMINAL
            or task.details_cleaned
            or task.closed_at_ms is None
            or task.pending
            or any(
                [
                    await session.scalar(select(model.id).where(model.task_id == task_id).limit(1))
                    is not None
                    for model in (
                        AutoOutboundSourceMember,
                        AutoOutboundPlate,
                        AutoOutboundDecisionRound,
                    )
                ]
            )
        ):
            return False
        proof = {}
        if task.status == "CANCELLED":
            if task.plan_revision != 0:
                return False
            inputs = tuple(
                await session.scalars(select(AutoTaskInput).where(AutoTaskInput.task_id == task_id))
            )
            if not any(
                row.operation == "sorting.task.cancel@v1"
                and row.disposition == "APPLIED"
                and row.effective_request.get("cancel_scope") == "TASK"
                for row in inputs
            ):
                return False
            if task.prepare_accepted_at_ms is not None:
                if self.flow is None:
                    return False
                closed = await self.flow.cancelled_retention_closed_at(session, task, now_ms=now_ms)
            elif any(
                [
                    await session.scalar(select(model.id).where(model.task_id == task_id).limit(1))
                    is not None
                    for model in _DETAILS
                    if model is not AutoTaskInput
                ]
            ):
                return False
            else:
                closed = task.closed_at_ms
        elif self.flow is None or not await self.flow.retention_basis(session, task, now_ms=now_ms):
            return False
        else:
            closed = task.direct_obligations_closed_at_ms
        if closed is None or now_ms < max(closed, task.closed_at_ms) + RETENTION_MS:
            return False
        for job in await dispatch.jobs(
            session,
            work_line_id=task.work_line_id,
            source_kinds=("AUTO_INBOUND_SOURCE",),
            task_id=task_id,
            pending=True,
        ):
            demand, handoff = await self.racks.facts(session, job_id=job.id)
            if demand.state != "NONE" or handoff.state == "BLOCKED":
                return False
            coverage = (
                [asdict(row) for row in demand.coverage]
                if handoff.state == "CLEAR" and demand.coverage == handoff.coverage
                else _departure_coverage(job, demand.point)
            )
            if not coverage:
                return False
            proof[job.id] = {
                "closed_at": closed,
                "point": asdict(demand.point),
                "origin": canonical_digest(asdict(job.origin)),
                "position_action_id": job.position_action_id,
                "action_ids": [a.id for a in job.actions],
                "coverage": coverage,
                "position": job.actual_position,
                "face": job.actual_face,
            }
        order = await _physical_deletion_order(session, task_id)
        if order is None:
            return False
        await session.execute(
            update(AutoInboundPlate)
            .where(AutoInboundPlate.task_id == task_id)
            .values(final_decision_round_id=None)
        )
        await session.execute(
            update(AutoTaskInput)
            .where(AutoTaskInput.task_id == task_id)
            .values(canonical_input_id=None)
        )
        for leaves in order:
            for model in (AutoInboundPlate, AutoInboundDecisionRound):
                ids = [record_id for row_model, record_id in leaves if row_model is model]
                if ids:
                    await session.execute(
                        delete(model).where(model.task_id == task_id, model.id.in_(ids))
                    )
        for model in _DETAILS:
            if model not in (AutoInboundPlate, AutoInboundDecisionRound):
                await session.execute(delete(model).where(model.task_id == task_id))
        task.direct_obligations_closed_at_ms = closed
        task.rack_retention_basis = proof or None
        task.issued_data = {}
        task.confirmation_basis = None
        task.details_cleaned = True
        task.details_cleaned_at_ms = now_ms
        return True
