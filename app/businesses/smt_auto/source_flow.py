"""Original inbound rack candidates; caller owns the AUTO transaction."""

from dataclasses import asdict

from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import JobOrigin
from app.core.identifiers import uuid7
from app.integrations.wms_transport import TransportCaller
from app.models import WorkLine

from .locking import lock_line
from .models import AutoInboundSourceMember, AutoTask, AutoTaskInput
from .rack_ports import AutoRackPorts, RackAuthorization


class SourceFlow:
    def __init__(self, inbound, *, caller: TransportCaller):
        if not isinstance(caller, TransportCaller):
            raise ValueError("explicit transport caller required")
        self.inbound, self.caller = inbound, caller
        self.dispatch = inbound.dispatch
        self.ports = AutoRackPorts(self.dispatch, inbound=inbound)

    async def advance(self, session, *, task_id, now_ms):
        task = await session.get(AutoTask, task_id)
        if task is None or task.business_type != "inbound":
            return
        bindings = await lock_line(session, self.dispatch, task.work_line_id)
        jobs = list(
            await self.dispatch.jobs(
                session, work_line_id=task.work_line_id, source_kinds=("AUTO_INBOUND_SOURCE",)
            )
        )
        line = await session.get(WorkLine, task.work_line_id)
        binding = next((b for b in bindings.values() if b and b.device_role == "RACK_ARM"), None)
        if task.status not in ("PREPARING", "EXECUTING") or line is None or binding is None:
            for job in jobs:
                if job.origin.task_id == task_id:
                    await self.ports.advance(session, job_id=job.id, now_ms=now_ms)
            return
        points = [line.position_bindings.get(role) for role in ("INBOUND_LEFT", "INBOUND_RIGHT")]
        if any(not point for point in points) or len(set(points)) != 2:
            return
        members = list(
            await session.scalars(
                select(AutoInboundSourceMember)
                .where(AutoInboundSourceMember.task_id == task_id)
                .order_by(
                    AutoInboundSourceMember.plan_revision,
                    AutoInboundSourceMember.rack_array_index,
                    AutoInboundSourceMember.id,
                )
            )
        )
        for member in members:
            if (
                member.rack_job_id is not None
                or member.withdrawn_at_ms is not None
                or member.withdrawn_by_input_id is not None
            ):
                continue
            plan = await session.get(AutoTaskInput, member.plan_input_id)
            if (
                plan is None
                or plan.task_id != task_id
                or plan.disposition != "APPLIED"
                or plan.applied_plan_revision != member.plan_revision
                or member.rack_array_index
                >= len(plan.effective_request.get("added_source_racks", []))
                or plan.effective_request["added_source_racks"][member.rack_array_index]
                != {"rack_id": member.rack_id}
            ):
                continue
            jobs = list(
                await self.dispatch.jobs(
                    session, work_line_id=task.work_line_id, source_kinds=("AUTO_INBOUND_SOURCE",)
                )
            )
            same = [
                j
                for j in jobs
                if j.origin.task_id == task_id
                and j.origin.rack_id == member.rack_id
                and j.state == "ACTIVE"
                and not any(a.kind == "DEPART" for a in j.actions)
            ]
            if same:
                if len(same) != 1:
                    continue
                job = same[0]
                original = next(
                    (
                        m
                        for m in members
                        if m.rack_job_id == job.id and m.preparation_action_id is not None
                    ),
                    None,
                )
                if (
                    original is None
                    or job.origin.resources != asdict(binding)
                    or await self.inbound.work_plans.inbound_source_exhausted(
                        session,
                        task_id=task_id,
                        job_id=job.id,
                        preparation_action_id=original.preparation_action_id,
                    )
                ):
                    continue
                member.rack_job_id, member.preparation_action_id = (
                    job.id,
                    original.preparation_action_id,
                )
                await self.dispatch.note_facts_changed(session, job_ids=(job.id,))
                await self.ports.advance(session, job_id=job.id, now_ms=now_ms)
                continue
            # A frozen departure remains the cutoff for later members of the same rack.
            if any(
                j.origin.task_id == task_id
                and j.origin.rack_id == member.rack_id
                and j.state not in ("COMPLETED", "WITHDRAWN", "NOT_ARRIVED")
                for j in jobs
            ):
                continue
            occupied = {
                j.origin.point
                for j in jobs
                if j.state not in ("COMPLETED", "WITHDRAWN", "NOT_ARRIVED")
            }
            point = next((p for p in points if p not in occupied), None)
            if point is None:
                continue
            auth = RackAuthorization("INBOUND_MEMBER", member.id)
            origin = JobOrigin(
                "AUTO_INBOUND_SOURCE",
                auth.source_ref,
                task.work_line_id,
                member.rack_id,
                task_id,
                task.workline_code,
                point,
                None,
                self.caller,
                str(uuid7()),
                asdict(binding),
            )
            job_id = await self.ports.register(
                session, origin, authorization=auth, original_binding=binding
            )
            if job_id is not None:
                await self.ports.prepare(
                    session,
                    job_id=job_id,
                    kind="ENTER",
                    authorization=auth,
                    face=None,
                    original_binding=binding,
                )
                await self.ports.advance(session, job_id=job_id, now_ms=now_ms)
        for job in await self.dispatch.jobs(
            session, work_line_id=task.work_line_id, source_kinds=("AUTO_INBOUND_SOURCE",)
        ):
            if job.origin.task_id == task_id:
                await self.ports.advance(session, job_id=job.id, now_ms=now_ms)
        await session.flush()
