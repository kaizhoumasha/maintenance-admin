"""AUTO-owned current grants and point facts; caller owns SERIALIZABLE transaction.

The explicit original DeviceBinding snapshot is a structural resource contract. Formal
AUTO role/site admission and production fact writers are still required before activation.
"""

import math
import re
from dataclasses import asdict, dataclass
from typing import Literal

from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
)
from app.businesses._components.sorting_task.contracts import PrepareAccepted
from app.core.strict_json import canonical_digest
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.wms_transport.types import validate_face
from app.models import WorkLine
from app.reliable import ScopeRef
from app.services.device_bindings import resolve_binding

from .archive import archived_facts
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


@dataclass(frozen=True)
class RackAuthorization:
    kind: Literal["INBOUND_MEMBER", "OUTBOUND_MEMBER", "FIRST_TARGET", "PLATE_TARGET"]
    record_id: int

    @property
    def source_ref(self) -> str:
        return f"{self.kind}:{self.record_id}"


SOURCES = (
    "AUTO_INBOUND_SOURCE",
    "AUTO_OUTBOUND_RETURN",
    "AUTO_OUTBOUND_TARGET",
    "ROLLER_ORDINARY",
    "ROLLER_DRAIN",
    "MANUAL_RETURN",
    "MANUAL_TRANSFER",
)

AUTH_MODELS = {
    "INBOUND_MEMBER": AutoInboundSourceMember,
    "OUTBOUND_MEMBER": AutoOutboundSourceMember,
    "FIRST_TARGET": AutoTaskInput,
    "PLATE_TARGET": AutoOutboundPlate,
}
COLLECTIONS = (
    AutoTaskInput,
    AutoSupplyMember,
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoBinWork,
    AutoInboundPlate,
    AutoOutboundPlate,
    AutoInboundDecisionRound,
    AutoOutboundDecisionRound,
    AutoTaskRequest,
)


def _ref(row):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return FactRef(
        row.__tablename__,
        tuple(str(values[column.name]) for column in row.__table__.primary_key.columns),
        (canonical_digest(values),),
    )


def _accepted(task, requests):
    original = [r for r in requests if r.task_id == task.task_id and r.purpose == "PREPARE"]
    if len(original) != 1:
        return False
    request = original[0]
    if (
        task.prepare_accepted_at_ms is None
        or request.operation != "sorting.task.prepare@v1"
        or request.unresolved
        or request.consumed_at_ms is None
        or request.request_data != {"task_id": task.task_id, "workline_code": task.workline_code}
        or not isinstance(request.response_snapshot, dict)
    ):
        return False
    response = request.response_snapshot
    try:
        PrepareAccepted.parse(
            response.get("http_status"), response.get("code"), response.get("data")
        )
    except (ValueError, TypeError):
        return False
    return True


def _plans(task, inputs):
    rows = [r for r in inputs if r.task_id == task.task_id and r.applied_plan_revision is not None]
    return (
        task.plan_revision > 0
        and sorted(r.applied_plan_revision for r in rows) == list(range(1, task.plan_revision + 1))
        and all(
            r.disposition == "APPLIED" and r.operation == "sorting.task.plan_delta@v1" for r in rows
        )
        and not any(r.task_id == task.task_id and r.disposition == "BLOCKED" for r in inputs)
    )


def _ready(data, *, bin_source):
    if not isinstance(data, dict) or data.get("result") != "READY":
        return False
    fields = (
        {"cell_id", "cell_index", "bin_type", "reel_layer"}
        if bin_source
        else {"rack_id", "rack_face", "slot_id", "rack_layer", "rack_column"}
    )
    if set(data) != {"result", "reel_thickness", "reel_diameter", *fields}:
        return False
    codes = ("cell_id",) if bin_source else ("rack_id", "slot_id")
    if not all(
        isinstance(data[key], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", data[key])
        for key in codes
    ):
        return False
    integers = (
        ("cell_index", "bin_type", "reel_layer") if bin_source else ("rack_layer", "rack_column")
    )
    if not all(type(data[key]) is int for key in integers):
        return False
    if bin_source and (data["cell_index"] not in range(1, 8) or data["bin_type"] not in (2, 3, 6)):
        return False
    if not bin_source:
        try:
            validate_face(data["rack_face"])
        except ValueError:
            return False
    return all(
        type(data[key]) is int or type(data[key]) is float and math.isfinite(data[key])
        for key in ("reel_thickness", "reel_diameter")
    )


def _accept_target(data, *, bin_source):
    if not isinstance(data, dict) or data.get("result") != "ACCEPT":
        return False
    if (
        not {"result", "target_locator"}
        <= set(data)
        <= {"result", "target_locator", "target_preparation", "next_source_action"}
    ):
        return False
    target = data["target_locator"]
    if not isinstance(target, dict) or set(target) != {
        "type",
        "rack_id",
        "rack_face",
        "slot_id",
        "rack_layer",
        "rack_column",
    }:
        return False
    if target["type"] != "RACK_SLOT" or not all(
        type(target[key]) is int for key in ("rack_layer", "rack_column")
    ):
        return False
    if not all(
        isinstance(target[key], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", target[key])
        for key in ("rack_id", "slot_id")
    ):
        return False
    try:
        validate_face(target["rack_face"])
    except ValueError:
        return False
    return data.get("target_preparation") in (None, "ROTATE", "REPLACE")


class AutoRackPorts:
    def __init__(self, dispatch, *, inbound=None, outbound=None):
        if dispatch.owner_domain != "smt_auto":
            raise ValueError("AUTO rack owner required")
        self.dispatch = dispatch
        self.inbound = inbound
        self.outbound = outbound

    async def _snapshot(self, session, work_line_id, *, lock=False):
        query = (
            select(AutoTask).where(AutoTask.work_line_id == work_line_id).order_by(AutoTask.task_id)
        )
        if lock:
            query = query.with_for_update()
        tasks = list(await session.scalars(query.execution_options(populate_existing=True)))
        task_ids = [t.task_id for t in tasks]
        rows = {}
        for model in COLLECTIONS:
            query = select(model).where(model.task_id.in_(task_ids)).order_by(model.id)
            if model is AutoBinWork:
                query = select(model).where(model.work_line_id == work_line_id).order_by(model.id)
            if lock:
                query = query.with_for_update()
            rows[model] = list(
                await session.scalars(query.execution_options(populate_existing=True))
            )
        return tasks, rows

    async def facts(self, session, *, job_id):
        progress = await self.dispatch.progress(session, job_id=job_id)
        if progress is None:
            raise ValueError("original AUTO rack job missing")
        origin = progress.origin
        if origin.source_kind not in SOURCES[:3]:
            raise ValueError("original AUTO source required")
        point = PointRef(
            "smt_auto", origin.work_line_id, job_id, origin.rack_id, origin.point or ""
        )
        tasks, rows = await self._snapshot(session, origin.work_line_id)
        task = next((t for t in tasks if t.task_id == origin.task_id), None)
        if task is not None and (restored := archived_facts(task, progress, point)) is not None:
            return restored
        scope = (
            point.owner_domain,
            str(point.work_line_id),
            point.job_id,
            point.rack_id,
            point.point,
        )
        # A missing original task cannot anchor even an otherwise empty collection.
        anchor = (
            _ref(task)
            if task
            else FactRef("missing_task", (origin.task_id or "missing",), ("UNKNOWN",))
        )
        coverage = (
            Coverage("smt_auto_task", scope, anchor, tuple(_ref(t) for t in tasks)),
        ) + tuple(
            Coverage(model.__tablename__, scope, anchor, tuple(_ref(r) for r in rows[model]))
            for model in COLLECTIONS
        )
        demand, handoff = "UNKNOWN", "UNKNOWN"
        if task and origin.point and origin.workline_code == task.workline_code:
            valid = _plans(task, rows[AutoTaskInput]) and _accepted(task, rows[AutoTaskRequest])
            members = [
                r
                for model in (AutoInboundSourceMember, AutoOutboundSourceMember)
                for r in rows[model]
                if r.rack_job_id == job_id
            ]
            member_identity = bool(members) and all(
                r.task_id == task.task_id and r.rack_id == origin.rack_id for r in members
            )
            blocked_inputs = any(r.disposition == "BLOCKED" for r in rows[AutoTaskInput])
            # A consumed final target grant waiting for its own movement is not occupancy.
            waiting = []
            for plate in rows[AutoOutboundPlate]:
                if (
                    plate.task_id == task.task_id
                    and plate.target_rack_job_id in (None, job_id)
                    and plate.source_rack_job_id != job_id
                    and all(
                        getattr(plate, field) is None
                        for field in (
                            "put_call_key",
                            "put_command_code",
                            "put_result_ref",
                            "put_consumed_at_ms",
                        )
                    )
                ):
                    auth = RackAuthorization("PLATE_TARGET", plate.id)
                    target = plate.target_snapshot or {}
                    if await self._authorization(
                        session, origin, auth, kind="ROTATE", face=target.get("rack_face")
                    ):
                        waiting.append(plate)
            waiting_ids = {plate.id for plate in waiting}
            waiting_rounds = {plate.final_decision_round_id for plate in waiting}
            for plate in waiting:
                grant = next(
                    (r for r in rows[AutoOutboundDecisionRound] if r.id == plate.grant_round_id),
                    None,
                )
                if grant and not grant.unresolved and grant.consumed_at_ms is not None:
                    waiting_rounds.add(grant.id)
            waiting_bins = {plate.bin_work_id for plate in waiting if plate.bin_work_id is not None}
            inbound_open = self.inbound is None and bool(rows[AutoInboundPlate])
            if self.inbound is not None:
                inbound_open = any(
                    [
                        not await self.inbound.closed(session, plate)
                        for plate in rows[AutoInboundPlate]
                        if plate.source_rack_job_id == job_id
                    ]
                )
            closed_inbound_bins = set()
            if self.inbound is not None:
                for work in rows[AutoBinWork]:
                    if work.business_type == "inbound" and await self.inbound.bin_closed(
                        session, work
                    ):
                        closed_inbound_bins.add(work.id)
            physical = (
                inbound_open
                or (
                    bool(rows[AutoInboundDecisionRound])
                    if self.inbound is None
                    else any(r.unresolved for r in rows[AutoInboundDecisionRound])
                )
                or any(plate.id not in waiting_ids for plate in rows[AutoOutboundPlate])
                or any(
                    round_.id not in waiting_rounds for round_ in rows[AutoOutboundDecisionRound]
                )
                or any(
                    (bin_.id not in waiting_bins and bin_.id not in closed_inbound_bins)
                    or bin_.production_hold_reason is not None
                    for bin_ in rows[AutoBinWork]
                )
            )
            if origin.source_kind == "AUTO_INBOUND_SOURCE" and self.inbound is not None:
                physical = not await self.inbound.source_clear(session, job_id=job_id)
            if self.outbound is not None and origin.source_kind in (
                "AUTO_OUTBOUND_RETURN",
                "AUTO_OUTBOUND_TARGET",
            ):
                physical = not await (
                    self.outbound.source_clear(session, job_id=job_id)
                    if origin.source_kind == "AUTO_OUTBOUND_RETURN"
                    else self.outbound.target_clear(session, job_id=job_id)
                )
            unsafe_progress = progress.review_reason not in (None, "AWAITING_ORIGINAL_RESULT") or (
                progress.state not in ("ACTIVE", "RELEASING")
                if (origin.source_kind == "AUTO_INBOUND_SOURCE" and self.inbound is not None)
                or (
                    origin.source_kind in ("AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET")
                    and self.outbound is not None
                )
                else progress.release_requested or progress.state != "ACTIVE"
            )
            if valid and not blocked_inputs and not physical and not unsafe_progress:
                handoff = "CLEAR"
            elif physical:
                handoff = "BLOCKED"
            if valid and member_identity:
                active = [
                    r
                    for r in members
                    if r.withdrawn_by_input_id is None and r.withdrawn_at_ms is None
                ]
                if active:
                    demand = "CONTINUE"
                    if origin.source_kind == "AUTO_INBOUND_SOURCE" and self.inbound is not None:
                        if all(
                            [
                                await self.inbound.work_plans.inbound_source_exhausted(
                                    session,
                                    task_id=task.task_id,
                                    job_id=job_id,
                                    preparation_action_id=member.preparation_action_id,
                                )
                                for member in active
                            ]
                        ):
                            demand = "NONE"
                elif (
                    not physical
                    and not blocked_inputs
                    and all(self._withdrawn(r, rows[AutoTaskInput]) for r in members)
                ):
                    demand = "NONE"
            if valid and origin.source_kind == "AUTO_OUTBOUND_TARGET":
                original = [
                    r
                    for r in rows[AutoTaskInput]
                    if r.task_id == task.task_id
                    and r.applied_plan_revision == 1
                    and r.target_job_id == job_id
                ]
                if len(original) == 1:
                    target = original[0].effective_request.get("target_rack", {})
                    if (
                        target.get("rack_id") == origin.rack_id
                        and target.get("rack_face") == origin.initial_face
                    ):
                        demand = "CONTINUE"
            if (
                self.outbound is not None
                and valid
                and origin.source_kind == "AUTO_OUTBOUND_RETURN"
                and members
            ):
                if all(
                    [
                        await self.outbound.face_source_terminal(session, m)
                        or self._withdrawn(m, rows[AutoTaskInput])
                        for m in members
                    ]
                ):
                    demand = "NONE"
                    if any(
                        m.task_id == task.task_id
                        and m.rack_id == origin.rack_id
                        and m.rack_job_id is None
                        and m.withdrawn_at_ms is None
                        and m.withdrawn_by_input_id is None
                        for m in rows[AutoOutboundSourceMember]
                    ):
                        demand = "CONTINUE"

            if self.outbound is not None and valid and origin.source_kind == "AUTO_OUTBOUND_TARGET":
                demand = await self.outbound.target_demand(session, job=progress, rows=rows)
            if waiting:
                demand = "CONTINUE"
            if blocked_inputs:
                demand = "UNKNOWN"
        return PointDemand(point, demand, coverage), DirectHandoff(point, handoff, coverage)

    @staticmethod
    def _withdrawn(member, inputs):
        key = (
            "source_racks" if isinstance(member, AutoInboundSourceMember) else "direct_pick_sources"
        )
        for row in inputs:
            if (
                row.id == member.withdrawn_by_input_id
                and row.task_id == member.task_id
                and member.withdrawn_at_ms is not None
                and row.disposition == "APPLIED"
                and row.operation == "sorting.task.cancel@v1"
                and row.effective_request.get("cancel_scope") == "PLAN_MEMBERS"
            ):
                return any(
                    selector.get("plan_revision") == member.plan_revision
                    and selector.get("rack_id") == member.rack_id
                    and (
                        isinstance(member, AutoInboundSourceMember)
                        or selector.get("rack_face") == member.rack_face
                    )
                    for selector in row.effective_request.get(key, [])
                )
        return False

    async def _lock_line_jobs(self, session, work_line_id):
        jobs = await self.dispatch.jobs(session, work_line_id=work_line_id, source_kinds=SOURCES)
        await self.dispatch.lock_jobs(session, job_ids=tuple(sorted(job.id for job in jobs)))

    async def _authorization(self, session, origin, authorization, *, kind, face):
        model = AUTH_MODELS.get(authorization.kind)
        if model is None or authorization.source_ref != origin.source_ref and kind == "ENTER":
            return None
        row = await session.get(model, authorization.record_id, populate_existing=True)
        if row is None or row.task_id != origin.task_id:
            return None
        if authorization.kind in ("INBOUND_MEMBER", "OUTBOUND_MEMBER"):
            expected = (
                "AUTO_INBOUND_SOURCE"
                if authorization.kind == "INBOUND_MEMBER"
                else "AUTO_OUTBOUND_RETURN"
            )
            plan = await session.get(AutoTaskInput, row.plan_input_id, populate_existing=True)
            if (
                origin.source_kind != expected
                or row.rack_id != origin.rack_id
                or row.withdrawn_by_input_id is not None
                or row.withdrawn_at_ms is not None
                or plan is None
                or plan.task_id != row.task_id
                or plan.disposition != "APPLIED"
                or plan.applied_plan_revision != row.plan_revision
                or face != getattr(row, "rack_face", None)
            ):
                return None
        elif authorization.kind == "FIRST_TARGET":
            target = row.effective_request.get("target_rack", {})
            if (
                kind != "ENTER"
                or origin.source_kind != "AUTO_OUTBOUND_TARGET"
                or row.applied_plan_revision != 1
                or row.disposition != "APPLIED"
                or target.get("rack_id") != origin.rack_id
                or target.get("rack_face") != face
            ):
                return None
        else:
            round_ = (
                await session.get(
                    AutoOutboundDecisionRound, row.final_decision_round_id, populate_existing=True
                )
                if row.final_decision_round_id
                else None
            )
            response = round_.response_snapshot if round_ else None
            data = response.get("data", {}) if isinstance(response, dict) else {}
            if not _accept_target(data, bin_source=row.bin_work_id is not None):
                return None
            target = data["target_locator"]
            if (
                origin.source_kind != "AUTO_OUTBOUND_TARGET"
                or round_ is None
                or round_.task_id != row.task_id
                or round_.plate_id != row.id
                or round_.purpose != "MATERIAL_DECIDE"
                or round_.operation
                != (
                    "outbound.bin.material.decide@v1"
                    if row.bin_work_id is not None
                    else "outbound.rackslot.material.decide@v1"
                )
                or response.get("http_status") != 200
                or response.get("code") != "DECIDED"
                or round_.unresolved
                or round_.consumed_at_ms is None
                or data.get("result") != "ACCEPT"
                or data.get("target_preparation") != ("ROTATE" if kind == "ROTATE" else "REPLACE")
                or row.target_snapshot != target
                or row.next_source_action != data.get("next_source_action")
                or target.get("rack_id") != origin.rack_id
                or target.get("rack_face") != face
            ):
                return None
            if self.outbound is not None:
                if not await self.outbound._grant(session, row):
                    return None
                response = await self.outbound._response(session, round_)
                pick = await self.outbound._result(session, row, "pick")
                request = {key: value for key, value in row.source_locator.items() if key != "type"}
                request.update(
                    task_id=row.task_id, barcode=row.barcode, scanned_at=row.scanned_at_ms
                )
                if (
                    response is None
                    or response.response_data != data
                    or pick is None
                    or round_.request_data != request
                ):
                    return None
            else:
                grant = (
                    await session.get(
                        AutoOutboundDecisionRound, row.grant_round_id, populate_existing=True
                    )
                    if row.grant_round_id
                    else None
                )
                if (
                    row.grant_kind != "READY"
                    or grant is None
                    or grant.task_id != row.task_id
                    or grant.purpose != "WORK_PLAN"
                    or not isinstance(grant.request_data, dict)
                    or grant.operation
                    != (
                        "outbound.bin.work_plan@v1"
                        if row.bin_work_id is not None
                        else "outbound.return_rack.work_plan@v1"
                    )
                    or grant.unresolved
                    or grant.consumed_at_ms is None
                    or not isinstance(grant.response_snapshot, dict)
                    or grant.response_snapshot.get("http_status") != 200
                    or grant.response_snapshot.get("code") != "DECIDED"
                    or not _ready(
                        grant.response_snapshot.get("data"), bin_source=row.bin_work_id is not None
                    )
                    or grant.response_snapshot.get("data") != row.grant_snapshot
                ):
                    return None
                if row.bin_work_id is not None:
                    subject = await session.get(
                        AutoBinWork, row.bin_work_id, populate_existing=True
                    )
                    if (
                        subject is None
                        or subject.task_id != row.task_id
                        or subject.work_line_id != origin.work_line_id
                        or grant.bin_work_id != subject.id
                        or subject.association != "TASK"
                        or subject.bin_code is None
                        or grant.request_data
                        != {
                            "task_id": row.task_id,
                            "bin_code": subject.bin_code,
                            "scanned_at": subject.scanned_at,
                        }
                    ):
                        return None
                else:
                    subject = await session.get(
                        AutoOutboundSourceMember, row.source_member_id, populate_existing=True
                    )
                    if (
                        subject is None
                        or subject.task_id != row.task_id
                        or subject.rack_job_id != row.source_rack_job_id
                        or subject.preparation_action_id != row.source_preparation_action_id
                        or grant.source_preparation_action_id != row.source_preparation_action_id
                        or set(grant.request_data)
                        != {"task_id", "rack_id", "rack_face", "arrived_at"}
                        or grant.request_data.get("task_id") != row.task_id
                        or grant.request_data.get("rack_id") != subject.rack_id
                        or grant.request_data.get("rack_face") != subject.rack_face
                        or type(grant.request_data.get("arrived_at")) is not int
                        or grant.request_data["arrived_at"] < 0
                        or row.grant_snapshot.get("rack_id") != subject.rack_id
                        or row.grant_snapshot.get("rack_face") != subject.rack_face
                    ):
                        return None
                    source = await self.dispatch.progress(session, job_id=row.source_rack_job_id)
                    preparation = (
                        next(
                            (
                                action
                                for action in source.actions
                                if action.id == row.source_preparation_action_id
                            ),
                            None,
                        )
                        if source
                        else None
                    )
                    point = (
                        {"kind": "RACK_POSITION", "location_code": source.origin.point}
                        if source
                        else None
                    )
                    if (
                        source is None
                        or preparation is None
                        or source.origin.source_kind != "AUTO_OUTBOUND_RETURN"
                        or source.origin.rack_id != subject.rack_id
                        or source.origin.workline_code != origin.workline_code
                        or not source.origin.point
                        or preparation.job_id != source.id
                        or preparation.kind not in ("ENTER", "ROTATE")
                        or preparation.phase != "APPLIED"
                        or not preparation.outcome
                        or preparation.outcome.get("status") != "SUCCEEDED"
                        or preparation.source_ref
                        != RackAuthorization("OUTBOUND_MEMBER", subject.id).source_ref
                        or preparation.target != point
                        or preparation.target_face != subject.rack_face
                        or preparation.actual_position != point
                        or preparation.actual_face != subject.rack_face
                        or source.actual_position != point
                        or source.actual_face != subject.rack_face
                        or source.origin.task_id != row.task_id
                        or source.origin.work_line_id != origin.work_line_id
                        or source.position_action_id != row.source_preparation_action_id
                        or source.current_action_id != source.position_action_id
                        or source.review_reason is not None
                        or source.state != "ACTIVE"
                        or source.release_requested
                    ):
                        return None
                if (
                    not isinstance(row.barcode, str)
                    or not 1 <= len(row.barcode) <= 1000
                    or type(row.scanned_at_ms) is not int
                    or row.scanned_at_ms < 0
                ):
                    return None
                if row.bin_work_id is not None:
                    locator = {
                        "type": "BIN_CELL",
                        "bin_code": subject.bin_code,
                        "cell_id": row.grant_snapshot["cell_id"],
                    }
                else:
                    locator = {
                        "type": "RACK_SLOT",
                        "rack_id": subject.rack_id,
                        "rack_face": subject.rack_face,
                        "slot_id": row.grant_snapshot["slot_id"],
                    }
                request = {key: value for key, value in locator.items() if key != "type"}
                request.update(
                    task_id=row.task_id, barcode=row.barcode, scanned_at=row.scanned_at_ms
                )
                if row.source_locator != locator or round_.request_data != request:
                    return None

        return row

    async def register(
        self,
        session,
        origin: JobOrigin,
        *,
        authorization: RackAuthorization,
        original_binding: DeviceBinding,
    ):
        if not await self._resources(session, origin, original_binding):
            return None
        await self._lock_line_jobs(session, origin.work_line_id)
        tasks, rows = await self._snapshot(session, origin.work_line_id, lock=True)
        task = next((t for t in tasks if t.task_id == origin.task_id), None)
        if not self._current(task, origin, rows):
            return None
        row = await self._authorization(
            session, origin, authorization, kind="ENTER", face=origin.initial_face
        )
        if row is None:
            return None
        job_id = await self.dispatch.register(
            session, origin, scope=ScopeRef("smt_auto", "TASK", origin.task_id)
        )
        progress = await self.dispatch.progress(session, job_id=job_id)
        # Preserve the canonical cycle while checking every other frozen origin field.
        from dataclasses import replace

        if progress.origin != replace(origin, cycle_id=progress.origin.cycle_id):
            raise ValueError("original AUTO job identity drift")
        column = (
            "target_job_id"
            if isinstance(row, AutoTaskInput)
            else "target_rack_job_id"
            if isinstance(row, AutoOutboundPlate)
            else "rack_job_id"
        )
        if getattr(row, column) not in (None, job_id):
            raise ValueError("original AUTO job association cannot change")
        setattr(row, column, job_id)
        await self.dispatch.note_facts_changed(session, job_ids=(job_id,))
        await session.flush()
        return job_id

    @staticmethod
    def _current(task, origin, rows):
        return (
            task is not None
            and task.status in ("PREPARING", "EXECUTING")
            and task.closed_at_ms is None
            and task.workline_code == origin.workline_code
            and task.task_type == "AUTO"
            and task.business_type
            == ("inbound" if origin.source_kind == "AUTO_INBOUND_SOURCE" else "outbound")
            and _accepted(task, rows[AutoTaskRequest])
            and _plans(task, rows[AutoTaskInput])
        )

    async def _resources(self, session, origin, original_binding, *, locked_bindings=None):
        if not isinstance(original_binding, DeviceBinding) or origin.resources != asdict(
            original_binding
        ):
            return False
        line = await session.get(
            WorkLine,
            origin.work_line_id,
            with_for_update=locked_bindings is None,
            populate_existing=True,
        )
        if (
            line is None
            or not line.is_enabled
            or line.business_code != "smt_auto"
            or line.line_code != origin.workline_code
            or original_binding.work_line_id != line.id
            or original_binding.business_code != "smt_auto"
            or origin.point not in original_binding.position_bindings.values()
        ):
            return False
        current = (
            locked_bindings.get(original_binding.device_code)
            if locked_bindings is not None
            else await resolve_binding(session, original_binding.device_code)
        )
        return current == original_binding and origin.point in line.position_bindings.values()

    async def prepare(
        self,
        session,
        *,
        job_id,
        kind,
        authorization: RackAuthorization,
        face,
        original_binding: DeviceBinding,
    ):
        progress = await self.dispatch.progress(session, job_id=job_id)
        if progress is None or not await self._resources(
            session, progress.origin, original_binding
        ):
            return None
        await self._lock_line_jobs(session, progress.origin.work_line_id)
        tasks, rows = await self._snapshot(session, progress.origin.work_line_id, lock=True)
        task = next((t for t in tasks if t.task_id == progress.origin.task_id), None)
        if not self._current(task, progress.origin, rows):
            return None
        progress = await self.dispatch.progress(session, job_id=job_id)
        row = await self._authorization(
            session, progress.origin, authorization, kind=kind, face=face
        )
        if row is None or progress.state != "ACTIVE" or progress.release_requested:
            return None
        column = (
            "target_job_id"
            if isinstance(row, AutoTaskInput)
            else "target_rack_job_id"
            if isinstance(row, AutoOutboundPlate)
            else "rack_job_id"
        )
        if getattr(row, column) != job_id and not (
            kind == "ROTATE" and getattr(row, column) is None
        ):
            return None
        reference = (
            "target_preparation_action_id"
            if isinstance(row, (AutoTaskInput, AutoOutboundPlate))
            else "preparation_action_id"
        )
        original_id = getattr(row, reference)
        if original_id is not None:
            action = next((a for a in progress.actions if a.id == original_id), None)
            return (
                original_id
                if action
                and action.job_id == job_id
                and action.target
                == {"kind": "RACK_POSITION", "location_code": progress.origin.point}
                and (action.kind, action.source_ref, action.target_face)
                == (kind, authorization.source_ref, face)
                and action.phase != "WITHDRAWN"
                else None
            )
        if kind == "ROTATE":
            current = next(
                (action for action in progress.actions if action.id == progress.position_action_id),
                None,
            )
            if (
                current is None
                or current.id != progress.current_action_id
                or current.kind not in ("ENTER", "ROTATE")
                or current.phase != "APPLIED"
                or not current.outcome
                or current.outcome.get("status") != "SUCCEEDED"
                or progress.actual_position
                != {"kind": "RACK_POSITION", "location_code": progress.origin.point}
                or progress.actual_face != current.target_face
                or current.target_face == face
                or progress.review_reason is not None
            ):
                return None
        _, handoff = await self.facts(session, job_id=job_id)
        if handoff.state != "CLEAR":
            return None
        action_id = await self.dispatch.prepare(
            session, job_id=job_id, kind=kind, source_ref=authorization.source_ref, face=face
        )
        if action_id is not None:
            current = await self.dispatch.progress(session, job_id=job_id)
            action = next((a for a in current.actions if a.id == action_id), None)
            if action is None or (action.kind, action.source_ref, action.target_face) != (
                kind,
                authorization.source_ref,
                face,
            ):
                raise ValueError("original AUTO preparation identity drift")
            setattr(row, column, job_id)
            setattr(row, reference, action_id)
            await self.dispatch.note_facts_changed(session, job_ids=(job_id,))
            await session.flush()
        return action_id

    async def advance(self, session, *, job_id, now_ms, allow_new=True):
        # Recover original facts even when current resources forbid a new departure.
        before = await self.dispatch.progress(session, job_id=job_id)
        if before is None:
            return None
        locked_bindings = await lock_line(
            session, self.dispatch, before.origin.work_line_id, include_job_ids=(job_id,)
        )
        before = await self.dispatch.progress(session, job_id=job_id)
        try:
            binding = DeviceBinding(**before.origin.resources)
        except (TypeError, ValueError):
            binding = None
        allow_departure = allow_new and await self._resources(
            session, before.origin, binding, locked_bindings=locked_bindings
        )
        demand, handoff = await self.facts(session, job_id=job_id)
        progress = await self.dispatch.advance(
            session,
            job_id=job_id,
            demand=demand,
            handoff=handoff,
            now_ms=now_ms,
            allow_new_departure=allow_departure,
        )
        if progress is None or progress.origin.source_kind != "AUTO_OUTBOUND_RETURN":
            return progress
        action = next((a for a in progress.actions if a.id == progress.position_action_id), None)
        previously_applied = (
            {a.id for a in before.actions if a.phase == "APPLIED"} if before else set()
        )
        if action is not None and action.id in previously_applied:
            return progress
        point = {"kind": "RACK_POSITION", "location_code": progress.origin.point}
        if (
            type(now_ms) is not int
            or not 0 <= now_ms <= 2**63 - 1
            or not progress.origin.point
            or progress.state != "ACTIVE"
            or progress.release_requested
            # Applied position remains an original fact while predecessors gate new work.
            or progress.review_reason not in (None, "UNRESOLVED_PREDECESSOR")
            or action is None
            or action.kind not in ("ENTER", "ROTATE")
            or action.phase != "APPLIED"
            or not action.outcome
            or action.outcome.get("status") != "SUCCEEDED"
            or action.id != progress.current_action_id
            or action.job_id != progress.id
            or action.target != point
            or action.actual_position != point
            or progress.actual_position != point
            or action.actual_face != action.target_face
            or progress.actual_face != action.target_face
        ):
            return progress
        members = await session.scalars(
            select(AutoOutboundSourceMember)
            .where(
                AutoOutboundSourceMember.rack_job_id == progress.id,
                AutoOutboundSourceMember.preparation_action_id == action.id,
                AutoOutboundSourceMember.task_id == progress.origin.task_id,
                AutoOutboundSourceMember.rack_id == progress.origin.rack_id,
                AutoOutboundSourceMember.rack_face == action.target_face,
            )
            .order_by(AutoOutboundSourceMember.id)
        )
        changed = False
        for member in members:
            if (
                member.arrived_at_ms is None
                and action.source_ref == RackAuthorization("OUTBOUND_MEMBER", member.id).source_ref
            ):
                member.arrived_at_ms = now_ms
                changed = True
        if changed:
            await self.dispatch.note_facts_changed(session, job_ids=(job_id,))
            await session.flush()
        return progress
