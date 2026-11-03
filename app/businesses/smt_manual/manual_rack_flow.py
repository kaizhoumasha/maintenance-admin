"""Manual production facts used by the original shared rack jobs."""

import json
from time import time

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
    RackProgress,
)
from app.businesses.runtime import Capabilities
from app.core.identifiers import uuid7
from app.integrations.wms_callback import Accepted, CallbackContext, Outcome, Rejected, Unavailable
from app.integrations.wms_transport import TransportCaller
from app.models import WorkLine

from .contracts import DirectPickCompleted, ReturnRackCompleted
from .models import BIN_COMPLETION_OPERATION, RACK_COMPLETION_OPERATIONS
from .repository import TaskRepository

MANUAL_SOURCES = ("MANUAL_RETURN", "MANUAL_TRANSFER")
REPORT = "outbound.return_rack.arrival_report@v1"


def _arrived(job, action):
    target = {"kind": "RACK_POSITION", "location_code": job.origin.point}
    return (
        action
        and action.kind in ("ENTER", "ROTATE")
        and action.phase == "APPLIED"
        and action.outcome
        and action.outcome["status"] == "SUCCEEDED"
        and action.outcome["transport_task_id"] == action.transport_task_id
        and action.outcome["rack_id"] == job.origin.rack_id
        and action.outcome_revision == action.position_revision
        and action.target == action.actual_position == target
        and action.target_face == action.actual_face
    )


def _planned_members(plans):
    members = set()
    for revision, plan in plans.items():
        for pick in plan["payload"].get("added_direct_picks") or ():
            source = pick["source_locator"]
            members.add(
                (revision, "DIRECT", source["rack_id"], source["rack_face"], source["slot_id"])
            )
        for source in plan["payload"].get("added_bin_source_racks") or ():
            for face in source["rack_face"]:
                members.add((revision, "BIN", source["rack_id"], face, ""))
    return members


def _member_key(member):
    return tuple(member[k] for k in ("plan_revision", "kind", "rack_id", "rack_face", "slot_id"))


def _withdrawn(member, notice):
    if (
        not notice
        or notice["operation"] != "sorting.task.cancel@v1"
        or notice["state"] != "APPLIED"
        or notice["payload"].get("cancel_scope") != "PLAN_MEMBERS"
    ):
        return False
    data = notice["payload"]
    if member["kind"] == "DIRECT":
        return any(
            r["plan_revision"] == member["plan_revision"]
            and r["rack_id"] == member["rack_id"]
            and r["rack_face"] == member["rack_face"]
            and (r.get("slot_ids") is None or member["slot_id"] in r["slot_ids"])
            for r in data.get("direct_pick_sources") or ()
        )
    return any(
        r["plan_revision"] == member["plan_revision"]
        and r["rack_id"] == member["rack_id"]
        and member["rack_face"] in r["rack_face"]
        for r in data.get("bin_source_racks") or ()
    )


class ManualRackFlow:
    def __init__(self, capabilities: Capabilities, roller, *, repository: TaskRepository):
        self.capabilities = capabilities
        self.roller = roller
        self.dispatch = roller.rack_dispatch
        self.repository = repository
        self.tables = repository.tables

    async def _rows(self, session, key, *where):
        table = self.tables[key]
        return (
            (await session.execute(select(table).where(*where).order_by(*table.primary_key)))
            .mappings()
            .all()
        )

    async def original_target_plan(self, session, task):
        inputs = self.tables["input"]
        rows = await self._rows(
            session,
            "input",
            inputs.c.task_id == task["task_id"],
            inputs.c.work_line_id == task["work_line_id"],
            inputs.c.is_plan_record,
            inputs.c.operation == "sorting.task.plan_delta@v1",
            inputs.c.state == "APPLIED",
            inputs.c.plan_revision == 1,
        )
        if (
            len(rows) == 1
            and rows[0]["payload"].get("task_id") == task["task_id"]
            and rows[0]["payload"].get("plan_revision") == 1
            and rows[0]["payload"].get("target_rack") == task["target_rack"]
        ):
            return rows[0]
        return None

    async def _current_preparation(self, session, job, action):
        if not (
            _arrived(job, action)
            and not job.review_reason
            and job.state == "ACTIVE"
            and job.position_action_id == job.current_action_id == action.id
            and job.actual_position == action.actual_position
            and job.actual_face == action.actual_face
        ):
            return False
        for original in job.actions:
            result = await self.dispatch.final_result(session, job_id=job.id, action_id=original.id)
            if not (
                original.phase == "APPLIED"
                and result
                and result.closure == "CLOSED"
                and not result.has_pending_outcome
                and result.outcome_revision == original.outcome_revision
                and result.outcome == original.outcome
                and result.position_observation == original.position_observation
            ):
                return False
        return True

    async def target_permission(self, session, task, plan):
        """Read only the applied target plan's linked original preparation."""
        if (
            not plan
            or not plan["target_job_id"]
            or not plan["target_action_id"]
            or await self.repository.has_pending_plan(session, task["task_id"])
        ):
            return None
        job = await self.dispatch.progress(session, job_id=plan["target_job_id"])
        if not (
            job
            and job.pending
            and job.state == "ACTIVE"
            and not job.review_reason
            and not job.release_requested
            and job.origin.source_kind == "MANUAL_TRANSFER"
            and job.origin.task_id == job.origin.source_ref == task["task_id"]
            and job.origin.work_line_id == task["work_line_id"]
            and job.origin.resources
            and job.origin.resources.get("target_plan")
            == [plan["operation"], plan["operation_id"], plan["plan_revision"]]
            and job.origin.rack_id == task["target_rack"]["rack_id"]
            and job.current_action_id == job.position_action_id == plan["target_action_id"]
        ):
            return None
        action = next((a for a in job.actions if a.id == job.position_action_id), None)
        if not (
            await self._current_preparation(session, job, action)
            and action.kind == "ENTER"
            and action.source_ref in (job.origin.source_ref, job.origin.cycle_id)
            and job.actual_face == task["target_rack"]["rack_face"]
        ):
            return None
        line = await session.get(WorkLine, task["work_line_id"])
        if not (
            task["status"] == "EXECUTING"
            and line
            and line.is_enabled
            and line.business_code == "smt_manual"
            and line.line_code == job.origin.workline_code
            and line.position_bindings.get("TRANSFER_RACK") == job.origin.point
            and not await self.dispatch.has_position_conflict(session, point=job.origin.point)
        ):
            return None
        jobs = await self.dispatch.jobs(
            session, work_line_id=job.origin.work_line_id, source_kinds=MANUAL_SOURCES, pending=True
        )
        if any(
            j.id != job.id
            and (j.origin.point == job.origin.point or j.origin.rack_id == job.origin.rack_id)
            for j in jobs
        ):
            return None
        return job.id, action.id

    async def reconcile_task(self, session, *, task_id):
        task = await self.repository.task(session, task_id)
        if not task:
            return
        line = await session.get(WorkLine, task["work_line_id"])
        await self.apply_completions(session, task=task)
        if not (
            line
            and line.is_enabled
            and line.business_code == "smt_manual"
            and line.line_code == task["workline_code"]
            and task["status"] == "EXECUTING"
            and task["business_type"] == "outbound"
            and task["prepare_accepted"]
        ):
            return
        jobs = await self.dispatch.jobs(
            session, work_line_id=line.id, source_kinds=MANUAL_SOURCES, pending=True
        )
        members = self.tables["member"]
        unbound = sorted(
            await self._rows(
                session,
                "member",
                members.c.task_id == task_id,
                members.c.kind == "DIRECT",
                members.c.status == "PENDING",
                members.c.rack_work_id.is_(None),
            ),
            key=lambda m: (m["plan_revision"], m["member_order"], m["id"]),
        )
        inputs = self.tables["input"]
        plans = {
            r["plan_revision"]: r
            for r in await self._rows(
                session,
                "input",
                inputs.c.task_id == task_id,
                inputs.c.work_line_id == line.id,
                inputs.c.is_plan_record,
            )
        }
        if await self.repository.has_pending_plan(session, task_id):
            return
        planned = _planned_members(plans)
        unbound = [m for m in unbound if _member_key(m) in planned]
        for role in ("TRANSFER", "RETURN"):
            point = line.position_bindings.get(role + "_RACK")
            if not point or await self.dispatch.has_position_conflict(session, point=point):
                continue
            kind = "MANUAL_" + role
            occupied = [
                j
                for j in jobs
                if j.pending and (j.origin.point == point or j.origin.source_kind == kind)
            ]
            if occupied and (
                len(occupied) != 1
                or occupied[0].origin.task_id != task_id
                or occupied[0].origin.point != point
                or occupied[0].origin.source_kind != kind
                or occupied[0].state != "ACTIVE"
            ):
                continue
            job = occupied[0] if occupied else None
            selected = []
            if role == "TRANSFER":
                if not task["target_rack"]:
                    continue
                plan = await self.original_target_plan(session, task)
                if not plan:
                    continue
                if job or plan["target_job_id"] or plan["target_action_id"]:
                    continue
                rack_id, face = (task["target_rack"][k] for k in ("rack_id", "rack_face"))
                source = task_id
            else:
                if not unbound:
                    continue
                if job:
                    entry = next((a for a in job.actions if a.kind == "ENTER"), None)
                    triggers = await self._rows(
                        session, "member", members.c.id == job.origin.source_ref
                    )
                    if job.origin.source_ref == f"legacy/manual/{job.id}" or not any(
                        m["rack_work_id"] == job.id
                        and m["task_id"] == task_id
                        and m["kind"] == "DIRECT"
                        and m["rack_id"] == job.origin.rack_id
                        and m["rack_face"] == job.origin.initial_face
                        and _member_key(m) in planned
                        and plans[m["plan_revision"]]["state"] == "APPLIED"
                        and (entry is None or m["preparation_action_id"] == entry.id)
                        for m in triggers
                    ):
                        continue
                rack_id = job.origin.rack_id if job else unbound[0]["rack_id"]
                selected = [m for m in unbound if m["rack_id"] == rack_id]
                if not selected:
                    continue
                face, source = selected[0]["rack_face"], selected[0]["id"]
            if any(
                j.pending and j.origin.rack_id == rack_id and (job is None or j.id != job.id)
                for j in jobs
            ):
                continue
            if job is None:
                if await self.dispatch.jobs(
                    session,
                    work_line_id=line.id,
                    source_kinds=(kind,),
                    task_id=task_id,
                    source_ref=source,
                ):
                    continue
                job_id = await self.dispatch.register(
                    session,
                    JobOrigin(
                        kind,
                        source,
                        line.id,
                        rack_id,
                        task_id,
                        line.line_code,
                        point,
                        face,
                        TransportCaller(str(line.id), None),
                        str(uuid7()),
                        {
                            role + "_RACK": point,
                            **(
                                {
                                    "target_plan": [
                                        plan["operation"],
                                        plan["operation_id"],
                                        plan["plan_revision"],
                                    ]
                                }
                                if role == "TRANSFER"
                                else {}
                            ),
                        },
                    ),
                )
                action_id = await self.dispatch.prepare(
                    session, job_id=job_id, kind="ENTER", source_ref=source, face=face
                )
            else:
                job_id = job.id
                position = next((a for a in job.actions if a.id == job.current_action_id), None)
                face = position.target_face if position else None
                action_id = (
                    position.id
                    if position
                    and position.kind in ("ENTER", "ROTATE")
                    and (
                        position.phase == "REGISTERED"
                        or await self._current_preparation(session, job, position)
                    )
                    else None
                )
            if job is None:
                jobs = (*jobs, await self.dispatch.progress(session, job_id=job_id))
            if role == "TRANSFER" and action_id:
                await session.execute(
                    inputs.update()
                    .where(
                        inputs.c.operation == plan["operation"],
                        inputs.c.operation_id == plan["operation_id"],
                    )
                    .values(target_job_id=job_id, target_action_id=action_id)
                )
            for member in selected:
                await session.execute(
                    members.update()
                    .where(members.c.id == member["id"])
                    .values(
                        rack_work_id=job_id,
                        preparation_action_id=action_id if member["rack_face"] == face else None,
                    )
                )

    async def get_pending_upper_id(self, session, *, kind="MANUAL_RACK"):
        if kind == "MANUAL_RACK":
            return await self.dispatch.pending_upper_id(session, source_kinds=MANUAL_SOURCES)
        q = self._report_candidates()
        return await session.scalar(
            q.order_by(self.tables["request"].c.operation_id.desc()).limit(1)
        )

    def _report_candidates(self):
        r = self.tables["request"]
        return select(r.c.operation_id).where(
            r.c.operation == REPORT, r.c.kind == "RACK_ARRIVAL_REPORT", r.c.state != "CONSUMED"
        )

    async def list_pending(
        self, session, *, upper_id, after_id=None, limit=100, kind="MANUAL_RACK"
    ):
        if kind == "MANUAL_RACK":
            return await self.dispatch.pending_ids(
                session,
                source_kinds=MANUAL_SOURCES,
                after_id=after_id,
                upper_id=upper_id,
                limit=limit,
            )
        identity = self.tables["request"].c.operation_id
        q = self._report_candidates().where(identity <= upper_id)
        if after_id is not None:
            q = q.where(identity > after_id)
        return tuple(await session.scalars(q.order_by(identity).limit(limit)))

    async def has_report_obligations(self, session, *, task_id):
        task = await self.repository.task(session, task_id)
        if not task:
            return True
        reports = self.tables["request"]
        rows = await self._rows(
            session,
            "request",
            reports.c.task_id == task_id,
            reports.c.kind == "RACK_ARRIVAL_REPORT",
        )
        members = self.tables["member"]
        recorded = await self._rows(
            session,
            "member",
            members.c.task_id == task_id,
            members.c.arrival_report_operation_id.is_not(None),
        )
        identities = {
            (r["operation_id"], r["rack_work_id"], r["action_id"])
            for r in rows
            if r["work_line_id"] == task["work_line_id"]
        }
        if any(
            (m["arrival_report_operation_id"], m["rack_work_id"], m["preparation_action_id"])
            not in identities
            for m in recorded
        ):
            return True
        if any(r["state"] != "CONSUMED" for r in rows):
            return True
        jobs = await self.dispatch.jobs(
            session,
            work_line_id=task["work_line_id"],
            source_kinds=("MANUAL_RETURN",),
            task_id=task_id,
        )
        for job in jobs:
            for a in job.actions:
                if a.kind != "ENTER" or (
                    a.phase == "APPLIED"
                    and a.outcome
                    and a.outcome["status"] in ("CANCELLED", "FAILED")
                ):
                    continue
                if not any(
                    r["rack_work_id"] == job.id
                    and r["action_id"] == a.id
                    and r["work_line_id"] == job.origin.work_line_id
                    for r in rows
                ):
                    return True
        return False

    def _report_source(self, job, action):
        member = self.tables["member"]
        return (
            (member.c.id == job.origin.source_ref)
            & (member.c.task_id == job.origin.task_id)
            & (member.c.rack_work_id == job.id)
            & (member.c.preparation_action_id == action.id)
        )

    async def _arrival_report(self, session, job, action):
        from .request.outbound_return_rack_arrival_report_v1 import ArrivalReport

        reports, members = self.tables["request"], self.tables["member"]
        owner = self._report_source(job, action)
        source = (await session.execute(select(members).where(owner))).mappings().one_or_none()
        if source is None or source["arrival_report_operation_id"] is not None:
            return
        if await session.scalar(
            select(reports.c.operation_id).where(
                reports.c.kind == "RACK_ARRIVAL_REPORT", reports.c.action_id == action.id
            )
        ):
            return
        payload = ArrivalReport(
            task_id=job.origin.task_id,
            transport_task_id=action.transport_task_id,
            outcome_revision=action.outcome_revision,
            rack_id=job.origin.rack_id,
            final_position=dict(type="RACK_POSITION", location_code=job.origin.point),
            arrival_face=action.actual_face,
        )
        operation_id = str(uuid7())
        message = await self.capabilities.wms.send(session, payload, operation_id=operation_id)
        await session.execute(
            reports.insert().values(
                kind="RACK_ARRIVAL_REPORT",
                operation=REPORT,
                operation_id=operation_id,
                task_id=job.origin.task_id,
                work_line_id=job.origin.work_line_id,
                request_snapshot=payload.model_dump(mode="json"),
                rack_work_id=job.id,
                action_id=action.id,
                message_id=message.message_id,
            )
        )

        await session.execute(
            members.update().where(owner).values(arrival_report_operation_id=operation_id)
        )

    async def advance_report(self, session: AsyncSession, *, operation_id: str) -> str:
        r = self.tables["request"]
        original = (
            (
                await session.execute(
                    select(r)
                    .where(
                        r.c.operation == REPORT,
                        r.c.kind == "RACK_ARRIVAL_REPORT",
                        r.c.operation_id == operation_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if original is None:
            return "NOT_FOUND"
        if original["state"] == "CONSUMED":
            return "CONSUMED"
        job = await self.dispatch.progress(session, job_id=original["rack_work_id"])
        task = await self.repository.task(session, original["task_id"])
        action = (
            next((a for a in job.actions if a.id == original["action_id"]), None) if job else None
        )
        snap = original["request_snapshot"] or {}
        if not (
            job
            and task
            and job.origin.source_kind == "MANUAL_RETURN"
            and action
            and action.kind == "ENTER"
            and task["work_line_id"] == job.origin.work_line_id == original["work_line_id"]
            and original["task_id"] == job.origin.task_id == snap.get("task_id")
            and action.transport_task_id == snap.get("transport_task_id")
            and type(snap.get("outcome_revision")) is int
            and action.outcome_revision is not None
            and 0 < snap["outcome_revision"] <= action.outcome_revision
            and job.origin.rack_id == snap.get("rack_id")
            and snap.get("final_position")
            == dict(type="RACK_POSITION", location_code=job.origin.point)
            and snap.get("arrival_face") == action.target_face
        ):
            return "OWNER_CONFLICT"
        member = self.tables["member"]
        marker = await session.scalar(
            select(member.c.arrival_report_operation_id).where(self._report_source(job, action))
        )
        if marker != original["operation_id"]:
            return "OWNER_CONFLICT"
        result = await self.capabilities.wms.get_result(
            session, message_id=original["message_id"], operation=REPORT, operation_id=operation_id
        )
        if (
            result.availability == "FOUND"
            and result.response_state == "VALID"
            and (result.message_id, result.operation, result.operation_id)
            == (original["message_id"], REPORT, operation_id)
            and (result.http_status, result.response_code)
            in {(200, "RECORDED"), (200, "DUPLICATE")}
            and result.response_data == {}
        ):
            await session.execute(
                r.update()
                .where(r.c.operation == REPORT, r.c.operation_id == operation_id)
                .values(
                    state="CONSUMED",
                    decision_code=result.response_code,
                    response=dict(
                        code=result.response_code, timestamp=result.response_timestamp, data={}
                    ),
                )
            )
            return "CONSUMED"
        return original["state"]

    async def evaluate_manual_point(
        self, session: AsyncSession, job: RackProgress
    ) -> tuple[PointDemand, DirectHandoff]:
        origin = job.origin
        point = PointRef("smt_manual", origin.work_line_id, job.id, origin.rack_id, origin.point)
        task_id = origin.task_id
        task = await self.repository.task(session, task_id)
        inputs = await self.roller.production.read_task_coverage(
            session, point=point, task_id=task_id
        )
        if not task or not inputs or task["business_type"] != "outbound":
            return PointDemand(point, "UNKNOWN", ()), DirectHandoff(point, "UNKNOWN", ())
        scope = inputs.scope
        status = FactRef("task_status", (task_id,), (task["status"],))
        coverage = [inputs]
        unknown = bool(inputs.rows) or not task["prepare_accepted"] or task["plan_revision"] < 1
        direct_unknown = False
        needed = blocked = False
        t, notices, bins, requests = (
            self.tables[k] for k in ("member", "input", "manual_bin", "request")
        )
        members = await self._rows(
            session, "member", (t.c.task_id == task_id) | (t.c.rack_work_id == job.id)
        )
        unknown |= any(m["task_id"] != task_id for m in members)
        original_inputs = await self._rows(session, "input", notices.c.task_id == task_id)
        plans = {
            r["plan_revision"]: r
            for r in original_inputs
            if r["is_plan_record"]
            and r["state"] == "APPLIED"
            and r["work_line_id"] == origin.work_line_id
        }
        unknown |= len(plans) != task["plan_revision"] or any(
            rev < 1 or rev > task["plan_revision"] for rev in plans
        )
        transfer = origin.source_kind == "MANUAL_TRANSFER"
        planned = _planned_members(plans)
        recorded = {_member_key(m) for m in members}
        missing_or_unplanned = planned ^ recorded
        unknown |= any(
            transfer or (kind == "DIRECT" and rack == origin.rack_id)
            for _, kind, rack, _, _ in missing_or_unplanned
        )
        root = any(a.kind == "DEPARTURE_DECIDE" for a in job.actions)
        if transfer:
            target_plan = plans.get(1)
            unknown |= not (
                target_plan
                and target_plan["target_job_id"] == job.id
                and origin.source_ref == task_id
                and origin.resources
                and origin.resources.get("target_plan")
                == [
                    target_plan["operation"],
                    target_plan["operation_id"],
                    target_plan["plan_revision"],
                ]
                and any(
                    a.id == target_plan["target_action_id"] and a.kind == "ENTER"
                    for a in job.actions
                )
                and target_plan["payload"].get("target_rack")
                == task["target_rack"]
                == dict(rack_id=origin.rack_id, rack_face=origin.initial_face)
            )
        else:
            entry = next((a for a in job.actions if a.kind == "ENTER"), None)
            unknown |= not any(
                (entry is None or m["preparation_action_id"] == entry.id)
                and m["id"] == origin.source_ref
                and m["rack_work_id"] == job.id
                and m["rack_id"] == origin.rack_id
                for m in members
            )
        competing = await self.dispatch.jobs(
            session, work_line_id=origin.work_line_id, source_kinds=MANUAL_SOURCES, pending=True
        )
        others = [
            j
            for j in competing
            if j.id != job.id
            and (j.origin.point == origin.point or j.origin.rack_id == origin.rack_id)
        ]
        direct_unknown |= bool(others)
        coverage.append(
            Coverage(
                "point_claims",
                scope,
                status,
                tuple(
                    FactRef(
                        "competing_job",
                        (j.id,),
                        (j.state, str(j.facts_revision), str(j.position_action_id)),
                    )
                    for j in others
                ),
            )
        )
        supply = (
            await self.roller.get_supply_progress(
                session, work_line_id=origin.work_line_id, task_id=task_id
            )
            if transfer
            else ()
        )
        if not transfer:
            for member in members:
                other_id = member["rack_work_id"]
                if (
                    member["task_id"] != task_id
                    or member["kind"] != "DIRECT"
                    or member["rack_id"] != origin.rack_id
                    or member["status"] not in ("PENDING", "STARTED")
                    or other_id in (None, job.id)
                ):
                    continue
                other = await self.dispatch.progress(session, job_id=other_id)
                if not (
                    other
                    and other.origin.source_kind == "MANUAL_RETURN"
                    and other.origin.task_id == task_id
                    and other.origin.work_line_id == origin.work_line_id
                    and other.origin.rack_id == origin.rack_id
                    and other.origin.point == origin.point
                    and any(
                        a.id == member["preparation_action_id"]
                        and a.kind in ("ENTER", "ROTATE")
                        and a.target_face == member["rack_face"]
                        for a in other.actions
                    )
                    and any(
                        m["id"] == other.origin.source_ref
                        and m["rack_work_id"] == other.id
                        and m["rack_id"] == origin.rack_id
                        and m["task_id"] == task_id
                        for m in members
                    )
                ):
                    unknown = True
                else:
                    coverage.append(
                        Coverage(
                            "distinct_return",
                            scope,
                            status,
                            (
                                FactRef(
                                    "member",
                                    (member["id"],),
                                    (
                                        other.id,
                                        member["preparation_action_id"],
                                        member["status"],
                                        str(member["plan_revision"]),
                                    ),
                                ),
                                FactRef(
                                    "original_job",
                                    (other.id,),
                                    (
                                        other.origin.source_ref,
                                        other.state,
                                        str(other.facts_revision),
                                    ),
                                ),
                            ),
                        )
                    )
        relevant = [
            m
            for m in members
            if transfer
            or (
                m["kind"] == "DIRECT"
                and m["rack_id"] == origin.rack_id
                and (m["rack_work_id"] == job.id or (not root and m["rack_work_id"] is None))
            )
        ]

        def record(kind, row, keys, fields):
            return FactRef(
                kind,
                tuple(str(row[k]) for k in keys),
                tuple(json.dumps(row[k], sort_keys=True) for k in fields),
            )

        coverage.append(
            Coverage(
                "plan_members",
                scope,
                status,
                tuple(
                    record(
                        "member",
                        m,
                        ("id",),
                        (
                            "task_id",
                            "plan_revision",
                            "kind",
                            "rack_id",
                            "rack_face",
                            "slot_id",
                            "status",
                            "cancelled_operation_id",
                            "rack_work_id",
                            "preparation_action_id",
                        ),
                    )
                    for m in relevant
                ),
            )
        )
        coverage.append(
            Coverage(
                "production_inputs",
                scope,
                inputs.anchor,
                tuple(
                    record(
                        "input",
                        r,
                        ("operation", "operation_id"),
                        (
                            "work_line_id",
                            "plan_revision",
                            "state",
                            "payload",
                            "target_job_id",
                            "target_action_id",
                        ),
                    )
                    for r in original_inputs
                    if r["is_plan_record"]
                    or r["operation"]
                    in (
                        *RACK_COMPLETION_OPERATIONS,
                        BIN_COMPLETION_OPERATION,
                        "sorting.task.cancel@v1",
                    )
                ),
            )
        )
        for m in relevant:
            if m["status"] == "CANCELLED":
                if not any(
                    n["operation_id"] == m["cancelled_operation_id"]
                    and n["work_line_id"] == origin.work_line_id
                    and _withdrawn(m, n)
                    for n in original_inputs
                ):
                    unknown = True
            if m["status"] not in {"PENDING", "STARTED", "CANCELLED", "COMPLETED"}:
                unknown = True
            if m["kind"] == "DIRECT":
                if m["status"] == "COMPLETED" and not any(
                    n["operation"] in RACK_COMPLETION_OPERATIONS
                    and n["state"] == "APPLIED"
                    and n["work_line_id"] == origin.work_line_id
                    and n["rack_id"] == m["rack_id"]
                    and (
                        (n["rack_face"] is None and n["plan_revision"] >= m["plan_revision"])
                        or (
                            n["rack_face"] == m["rack_face"]
                            and n["plan_revision"] == m["plan_revision"]
                        )
                    )
                    for n in original_inputs
                ):
                    unknown = True
                needed |= m["status"] in ("PENDING", "STARTED")
                blocked |= m["status"] == "STARTED"
                if m["preparation_action_id"] and not transfer:
                    direct_unknown |= not any(
                        a.id == m["preparation_action_id"] and a.target_face == m["rack_face"]
                        for a in job.actions
                    )
            elif m["kind"] == "BIN":
                matched = [
                    p
                    for p in supply
                    if p.task_id == task_id
                    and (p.source.plan_revision, p.source.rack_id, p.source.rack_face)
                    == (m["plan_revision"], m["rack_id"], m["rack_face"])
                ]
                if len(matched) != 1 or not matched[0].original_ids:
                    unknown = True
                else:
                    needed |= matched[0].pending or (
                        not matched[0].feed_complete and m["status"] != "CANCELLED"
                    )
            else:
                unknown = True
        for n in original_inputs:
            if n["state"] in ("REVIEW", "PENDING") and (
                (
                    n["operation"] in RACK_COMPLETION_OPERATIONS
                    and (transfer or n["rack_id"] == origin.rack_id)
                )
                or (transfer and n["operation"] == BIN_COMPLETION_OPERATION)
            ):
                unknown = direct_unknown = True
        if transfer:
            coverage.append(
                Coverage(
                    "supply",
                    scope,
                    status,
                    tuple(
                        FactRef(
                            "supply_face",
                            p.original_ids,
                            (
                                str(p.source.plan_revision),
                                p.source.rack_id,
                                p.source.rack_face,
                                str(p.feed_complete),
                                str(p.pending),
                            ),
                        )
                        for p in supply
                    ),
                )
            )
            arrivals = await self._rows(
                session,
                "manual_bin",
                (bins.c.task_id == task_id)
                | (bins.c.target_job_id == job.id)
                | (bins.c.work_line_id == origin.work_line_id)
                & bins.c.task_id.is_(None)
                & (bins.c.state != "HOLD"),
            )
            admissions = await self._rows(
                session,
                "request",
                requests.c.task_id == task_id,
                requests.c.kind == "BIN_ADMISSION",
            )
            coverage.append(
                Coverage(
                    "manual_bins",
                    scope,
                    status,
                    tuple(
                        record(
                            "manual_bin",
                            b,
                            ("arrival_id",),
                            (
                                "work_line_id",
                                "bin_code",
                                "state",
                                "admission_operation_id",
                                "completed_operation_id",
                                "command_code",
                                "target_job_id",
                                "target_action_id",
                            ),
                        )
                        for b in arrivals
                    ),
                )
            )
            coverage.append(
                Coverage(
                    "admissions",
                    scope,
                    status,
                    tuple(
                        record(
                            "admission",
                            r,
                            ("operation", "operation_id"),
                            ("work_line_id", "arrival_id", "message_id", "state", "decision_code"),
                        )
                        for r in admissions
                    ),
                )
            )
            arrival_ids = {b["arrival_id"] for b in arrivals}
            unknown |= any(
                r["arrival_id"] not in arrival_ids or r["work_line_id"] != origin.work_line_id
                for r in admissions
            )
            releases = []
            for b in arrivals:
                if b["state"] == "HOLD" and b["admission_operation_id"] is None:
                    continue
                if (
                    b["task_id"] != task_id
                    or b["work_line_id"] != origin.work_line_id
                    or (
                        b["state"] == "WORK_REQUIRED"
                        and (
                            not target_plan
                            or b["target_job_id"] != job.id
                            or b["target_action_id"] != target_plan["target_action_id"]
                        )
                    )
                    or (
                        b["admission_operation_id"]
                        and not any(
                            r["operation_id"] == b["admission_operation_id"]
                            and r["arrival_id"] == b["arrival_id"]
                            and r["work_line_id"] == origin.work_line_id
                            for r in admissions
                        )
                    )
                ):
                    direct_unknown = unknown = True
                if b["state"] == "RELEASED":
                    release = await self.repository.bin_release_evidence(session, self.roller, b)
                    if release is None:
                        direct_unknown = unknown = True
                    else:
                        releases.append(release)
                needed |= b["state"] != "RELEASED"
                blocked |= b["state"] == "WORK_REQUIRED"
            coverage.append(Coverage("bin_releases", scope, status, tuple(releases)))
        evidence = tuple(coverage)
        return (
            PointDemand(
                point, "UNKNOWN" if unknown else "CONTINUE" if needed else "NONE", evidence
            ),
            DirectHandoff(
                point,
                "UNKNOWN" if direct_unknown or unknown else "BLOCKED" if blocked else "CLEAR",
                evidence,
            ),
        )

    async def advance_rack(self, session, *, rack_work_id, now_ms=None):
        job = await self.dispatch.progress(session, job_id=rack_work_id)
        if not job or job.origin.source_kind not in MANUAL_SOURCES:
            return "NOT_FOUND"
        if not job.pending:
            return job.state
        task = await self.repository.task(session, job.origin.task_id)
        if not task or task["work_line_id"] != job.origin.work_line_id:
            return "OWNER_CONFLICT"
        await self.apply_completions(session, task=task)
        first_arrival = not any(a.kind == "ENTER" and _arrived(job, a) for a in job.actions)
        demand, handoff = await self.evaluate_manual_point(session, job)
        job = await self.dispatch.advance(
            session,
            job_id=job.id,
            demand=demand,
            handoff=handoff,
            now_ms=int(time() * 1000) if now_ms is None else now_ms,
        )
        entry = next((a for a in job.actions if a.kind == "ENTER"), None)
        if first_arrival and job.origin.source_kind == "MANUAL_RETURN" and _arrived(job, entry):
            await self._arrival_report(session, job, entry)
        if not job.pending:
            return job.state
        if not job.actions:
            return "WAITING_FOR_ENTRY"
        latest = job.actions[-1]
        if latest.kind == "DEPART":
            return "WAITING_FOR_DEPARTURE_RESULT"
        if latest.kind == "DEPARTURE_DECIDE":
            return "WAITING_FOR_DEPARTURE"
        if job.review_reason == "AWAITING_ORIGINAL_RESULT":
            return "WAITING_FOR_ENTRY" if latest.kind == "ENTER" else "WAITING_FOR_ROTATION"
        if job.review_reason:
            if latest.outcome and latest.outcome["position_unknown"] and not latest.actual_position:
                return "POSITION_UNKNOWN"
            return job.review_reason
        if demand.state == "UNKNOWN" or handoff.state == "UNKNOWN":
            return "WAITING_FOR_ORIGINAL_FACTS"
        if not _arrived(job, latest):
            return "RECOVERY_REQUIRED"
        if await self.dispatch.has_position_conflict(session, point=job.origin.point):
            return "POSITION_CONFLICT"
        if (
            handoff.state != "UNKNOWN"
            and job.origin.source_kind == "MANUAL_RETURN"
            and job.state == "ACTIVE"
            and demand.state == "CONTINUE"
            and job.position_action_id == latest.id
        ):
            t = self.tables["member"]
            remaining = sorted(
                await self._rows(
                    session,
                    "member",
                    t.c.task_id == task["task_id"],
                    t.c.rack_work_id == job.id,
                    t.c.status.in_(("PENDING", "STARTED")),
                ),
                key=lambda m: (m["plan_revision"], m["member_order"], m["id"]),
            )
            face_members = [m for m in remaining if m["rack_face"] == latest.actual_face]
            if any(m["preparation_action_id"] != latest.id for m in face_members):
                return "PREPARATION_CONFLICT"
            if (
                remaining
                and not face_members
                and not any(m["status"] == "STARTED" for m in remaining)
            ):
                line = await session.get(WorkLine, job.origin.work_line_id)
                if not (
                    line
                    and line.is_enabled
                    and line.business_code == "smt_manual"
                    and line.line_code == job.origin.workline_code
                    and task["status"] == "EXECUTING"
                    and line.position_bindings.get("RETURN_RACK") == job.origin.point
                ):
                    return "NEW_WORK_BLOCKED"
                next_member = remaining[0]
                action = await self.dispatch.prepare(
                    session,
                    job_id=job.id,
                    kind="ROTATE",
                    source_ref=next_member["id"],
                    face=next_member["rack_face"],
                )
                if action:
                    await session.execute(
                        t.update()
                        .where(
                            t.c.rack_work_id == job.id,
                            t.c.status == "PENDING",
                            t.c.rack_face == next_member["rack_face"],
                            t.c.preparation_action_id.is_(None),
                        )
                        .values(preparation_action_id=action)
                    )
                    return "WAITING_FOR_ROTATION"
        return "WORKING"

    async def accept_direct_pick_completed(
        self, ctx: CallbackContext, payload: DirectPickCompleted
    ) -> Outcome:
        return await self._accept_completed(ctx, payload, face=payload.rack_face)

    async def accept_return_rack_completed(
        self, ctx: CallbackContext, payload: ReturnRackCompleted
    ) -> Outcome:
        return await self._accept_completed(ctx, payload, face=None)

    async def _accept_completed(
        self,
        ctx: CallbackContext,
        payload: DirectPickCompleted | ReturnRackCompleted,
        *,
        face: str | None,
    ) -> Outcome:
        _, task, _ = await self.repository.original_dispatch_task(
            ctx.session, self.roller, payload.task_id
        )
        if task is None:
            raise Unavailable()
        if task["business_type"] != "outbound":
            raise Rejected("INVALID_DATA")
        table = self.tables["input"]
        await ctx.session.execute(
            table.insert().values(
                operation_id=ctx.operation_id,
                operation=ctx.operation,
                receipt_id=ctx.receipt_id,
                task_id=payload.task_id,
                work_line_id=task["work_line_id"],
                rack_id=payload.rack_id,
                plan_revision=payload.plan_revision
                if face is not None
                else payload.through_plan_revision,
                rack_face=face,
                completed_at=payload.completed_at,
                timestamp=ctx.timestamp,
                payload=payload.model_dump(mode="json"),
                state="PENDING",
            )
        )
        await self.apply_completions(ctx.session, task=task)
        await self.repository.note_received_work(
            ctx.session, task, operation=ctx.operation, record_id=ctx.operation_id
        )
        return Accepted()

    async def apply_completions(self, session: AsyncSession, *, task: RowMapping) -> None:
        """The caller already holds the original task's WorkLine/Device/task locks."""
        table, members = self.tables["input"], self.tables["member"]
        notices = (
            await session.execute(
                select(table).where(
                    table.c.task_id == task["task_id"],
                    table.c.operation.in_(RACK_COMPLETION_OPERATIONS),
                    table.c.state == "PENDING",
                )
            )
        ).mappings()
        for notice in notices:
            if task["plan_revision"] < notice["plan_revision"]:
                continue
            scope = [
                members.c.task_id == task["task_id"],
                members.c.rack_id == notice["rack_id"],
                members.c.kind == "DIRECT",
            ]
            if notice["rack_face"] is None:
                scope.append(members.c.plan_revision <= notice["plan_revision"])
            else:
                scope.extend(
                    (
                        members.c.plan_revision == notice["plan_revision"],
                        members.c.rack_face == notice["rack_face"],
                    )
                )
            await session.execute(
                members.update()
                .where(*scope, members.c.status.in_(("PENDING", "STARTED")))
                .values(status="COMPLETED")
            )
            await session.execute(
                table.update()
                .where(
                    table.c.operation == notice["operation"],
                    table.c.operation_id == notice["operation_id"],
                )
                .values(state="APPLIED")
            )
