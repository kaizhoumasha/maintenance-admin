"""Ordered RETURN and original TRANSFER grants through the public rack boundary."""

from dataclasses import asdict

from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import JobOrigin
from app.core.identifiers import uuid7
from app.integrations.wms_transport import TransportCaller
from app.models import WorkLine

from .locking import lock_line
from .models import (
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoTask,
    AutoTaskInput,
)
from .rack_ports import AutoRackPorts, RackAuthorization


class OutboundRacks:
    def __init__(self, outbound, *, station_id: str | None = None):
        self.outbound, self.station_id = outbound, station_id
        self.dispatch = outbound.dispatch
        self.ports = AutoRackPorts(self.dispatch, outbound=outbound)

    async def _enter(self, session, task, binding, *, source_kind, point, rack_id, face, auth):
        origin = JobOrigin(
            source_kind,
            auth.source_ref,
            task.work_line_id,
            rack_id,
            task.task_id,
            task.workline_code,
            point,
            face,
            TransportCaller(str(task.work_line_id), self.station_id),
            str(uuid7()),
            asdict(binding),
        )
        job_id = await self.ports.register(
            session, origin, authorization=auth, original_binding=binding
        )
        if job_id:
            await self.ports.prepare(
                session,
                job_id=job_id,
                kind="ENTER",
                authorization=auth,
                face=face,
                original_binding=binding,
            )
        return job_id

    async def advance(self, session, *, task_id, now_ms):
        task = await session.get(AutoTask, task_id)
        if task is None or task.business_type != "outbound":
            return
        bindings = await lock_line(session, self.dispatch, task.work_line_id)
        jobs = list(
            await self.dispatch.jobs(
                session,
                work_line_id=task.work_line_id,
                source_kinds=("AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET"),
            )
        )
        for job in jobs:
            if job.origin.task_id == task_id:
                await self.ports.advance(session, job_id=job.id, now_ms=now_ms)
        line = await session.get(WorkLine, task.work_line_id)
        binding = next((b for b in bindings.values() if b and b.device_role == "RACK_ARM"), None)
        if (
            task.status not in ("PREPARING", "EXECUTING")
            or line is None
            or not line.is_enabled
            or binding is None
        ):
            return
        points = line.position_bindings
        if (
            not points.get("RETURN_RACK")
            or not points.get("TRANSFER_RACK")
            or points["RETURN_RACK"] == points["TRANSFER_RACK"]
        ):
            return
        jobs = list(
            await self.dispatch.jobs(
                session,
                work_line_id=task.work_line_id,
                source_kinds=("AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET"),
            )
        )
        active = [j for j in jobs if j.state not in ("COMPLETED", "WITHDRAWN", "NOT_ARRIVED")]
        members = list(
            await session.scalars(
                select(AutoOutboundSourceMember)
                .where(AutoOutboundSourceMember.task_id == task_id)
                .order_by(
                    AutoOutboundSourceMember.plan_revision,
                    AutoOutboundSourceMember.rack_array_index,
                    AutoOutboundSourceMember.rack_face,
                    AutoOutboundSourceMember.id,
                )
            )
        )
        for member in members:
            if await self.outbound.face_source_terminal(session, member):
                if await self.outbound.face_closed(session, member):
                    member.terminal_basis = member.terminal_basis or dict(kind="NORMAL_REPORTED")
                    member.terminal_at_ms = member.terminal_at_ms or now_ms
                continue
            if member.withdrawn_at_ms is not None or member.withdrawn_by_input_id is not None:
                continue
            if member.rack_job_id:
                if member.arrived_at_ms is not None:
                    await self.outbound.work_plans.start_return_face(
                        session, work_line_id=task.work_line_id, member_id=member.id, now_ms=now_ms
                    )
                    await self.outbound.work_plans.consume_return_face(
                        session, work_line_id=task.work_line_id, member_id=member.id, now_ms=now_ms
                    )
                break
            auth = RackAuthorization("OUTBOUND_MEMBER", member.id)
            occupants = [j for j in active if j.origin.point == points["RETURN_RACK"]]
            if not occupants:
                await self._enter(
                    session,
                    task,
                    binding,
                    source_kind="AUTO_OUTBOUND_RETURN",
                    point=points["RETURN_RACK"],
                    rack_id=member.rack_id,
                    face=member.rack_face,
                    auth=auth,
                )
            elif len(occupants) == 1:
                job = occupants[0]
                earlier = [m for m in members if m.rack_job_id == job.id]
                if (
                    job.origin.task_id == task_id
                    and job.origin.rack_id == member.rack_id
                    and earlier
                    and all([await self.outbound.face_source_terminal(session, m) for m in earlier])
                    and not any(a.kind == "DEPART" for a in job.actions)
                ):
                    await self.ports.prepare(
                        session,
                        job_id=job.id,
                        kind="ROTATE",
                        authorization=auth,
                        face=member.rack_face,
                        original_binding=binding,
                    )
            break
        target_jobs = [j for j in active if j.origin.point == points["TRANSFER_RACK"]]
        first_target_registered = False
        first = await session.scalar(
            select(AutoTaskInput).where(
                AutoTaskInput.task_id == task_id,
                AutoTaskInput.applied_plan_revision == 1,
                AutoTaskInput.disposition == "APPLIED",
            )
        )
        if first and first.target_job_id is None and not target_jobs:
            target = first.effective_request.get("target_rack", {})
            if set(target) == {"rack_id", "rack_face"}:
                first_target_registered = bool(
                    await self._enter(
                        session,
                        task,
                        binding,
                        source_kind="AUTO_OUTBOUND_TARGET",
                        point=points["TRANSFER_RACK"],
                        rack_id=target["rack_id"],
                        face=target["rack_face"],
                        auth=RackAuthorization("FIRST_TARGET", first.id),
                    )
                )
        for plate in await session.scalars(
            select(AutoOutboundPlate)
            .where(
                AutoOutboundPlate.task_id == task_id,
                AutoOutboundPlate.final_decision_round_id.is_not(None),
                AutoOutboundPlate.put_call_key.is_(None),
            )
            .order_by(AutoOutboundPlate.id)
        ):
            if plate.source_arrival_snapshot.get("pending_platform_scan"):
                continue
            round_ = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
            data = (round_.response_snapshot or {}).get("data", {}) if round_ else {}
            preparation = data.get("target_preparation")
            if data.get("result") != "ACCEPT" or first_target_registered:
                continue
            target = data["target_locator"]
            auth = RackAuthorization("PLATE_TARGET", plate.id)
            if (
                preparation == "ROTATE"
                and len(target_jobs) == 1
                and target_jobs[0].origin.rack_id == target["rack_id"]
            ):
                await self.ports.prepare(
                    session,
                    job_id=target_jobs[0].id,
                    kind="ROTATE",
                    authorization=auth,
                    face=target["rack_face"],
                    original_binding=binding,
                )
            elif preparation in (None, "REPLACE") and not target_jobs:
                if not await self._enter(
                    session,
                    task,
                    binding,
                    source_kind="AUTO_OUTBOUND_TARGET",
                    point=points["TRANSFER_RACK"],
                    rack_id=target["rack_id"],
                    face=target["rack_face"],
                    auth=auth,
                ):
                    continue
            elif (
                preparation is None
                and any(
                    job.id == plate.target_rack_job_id and job.origin.source_ref == auth.source_ref
                    for job in target_jobs
                )
                and plate.target_preparation_action_id is None
            ):
                await self.ports.prepare(
                    session,
                    job_id=plate.target_rack_job_id,
                    kind="ENTER",
                    authorization=auth,
                    face=target["rack_face"],
                    original_binding=binding,
                )
            elif preparation is None:
                continue
            break
        for job in await self.dispatch.jobs(
            session,
            work_line_id=task.work_line_id,
            source_kinds=("AUTO_OUTBOUND_RETURN", "AUTO_OUTBOUND_TARGET"),
        ):
            if job.origin.task_id == task_id:
                await self.ports.advance(session, job_id=job.id, now_ms=now_ms)
        await session.flush()
