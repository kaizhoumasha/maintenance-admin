"""Caller-transactional rack dispatch. Only original WMS/Transport ports grant progress."""

import json
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.businesses.runtime import Capabilities
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.wms_transport import (
    OPERATION as TRANSPORT_OPERATION,
)
from app.integrations.wms_transport import (
    Rack,
    RackPosition,
    TransportCaller,
    TransportTaskResult,
    Zone,
)
from app.models import WorkLine, WorkLinePosition

from .contracts import (
    ActionProgress,
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
    RackProgress,
    TaskAdmission,
)
from .models import RackAction, RackJob


def _id():
    return str(uuid7())


def _point(code):
    return {"kind": "RACK_POSITION", "location_code": code}


def _caller(row):
    return TransportCaller(**row.caller) if row.caller else None


def _project(cls, row, **overrides):
    return cls(
        **{
            f.name: overrides[f.name] if f.name in overrides else deepcopy(getattr(row, f.name))
            for f in fields(cls)
        }
    )


def _snapshot(job, actions):
    origin = _project(JobOrigin, job, cycle_id=job.original_cycle_id, caller=_caller(job))
    return _project(
        RackProgress,
        job,
        origin=origin,
        actions=tuple(_project(ActionProgress, a, caller=_caller(a)) for a in actions),
    )


def _parts(value):
    return isinstance(value, tuple) and bool(value) and all(isinstance(v, str) and v for v in value)


def _json(value):
    return json.loads(json.dumps(value))


def _fact(value):
    return (
        isinstance(value, FactRef)
        and isinstance(value.kind, str)
        and bool(value.kind)
        and _parts(value.key)
        and _parts(value.version)
    )


def _covered(value, scope):
    return (
        isinstance(value, Coverage)
        and isinstance(value.collection, str)
        and bool(value.collection)
        and _parts(value.scope)
        and value.scope[: len(scope)] == scope
        and _fact(value.anchor)
        and isinstance(value.rows, tuple)
        and all(_fact(row) for row in value.rows)
    )


def _at_point(value, job):
    if not job.point:
        return False
    point = PointRef(job.owner_domain, job.work_line_id, job.id, job.rack_id, job.point)
    scope = (job.owner_domain, str(job.work_line_id), job.id, job.rack_id, job.point)
    return (
        isinstance(value, (PointDemand, DirectHandoff))
        and value.point == point
        and isinstance(value.coverage, tuple)
        and bool(value.coverage)
        and all(_covered(c, scope) for c in value.coverage)
    )


def _close(job, state, reason, basis, now_ms):
    job.state, job.pending = state, False
    job.closure_reason, job.closure_basis = reason, _json(basis)
    job.completed_at_ms, job.review_reason = now_ms, None


class RackDispatch:
    def __init__(
        self,
        owner_domain: str,
        capabilities: Capabilities,
        *,
        task_admission: TaskAdmission | None = None,
    ):
        self.owner_domain = owner_domain
        self.capabilities = capabilities
        self.task_admission = task_admission

    @property
    def _jobs(self):
        return (
            select(RackJob)
            .where(RackJob.owner_domain == self.owner_domain)
            .execution_options(populate_existing=True)
        )

    async def _job(self, session, job_id, *, lock=False):
        query = self._jobs.where(RackJob.id == job_id)
        if lock:
            query = query.with_for_update()
        return await session.scalar(query)

    async def lock_jobs(self, session: AsyncSession, *, job_ids: tuple[str, ...]) -> None:
        await session.execute(
            self._jobs.where(RackJob.id.in_(job_ids)).order_by(RackJob.id).with_for_update()
        )

    async def lock_actions(self, session: AsyncSession, *, job_ids: tuple[str, ...]) -> None:
        await session.execute(
            select(RackAction.id)
            .where(
                RackAction.owner_domain == self.owner_domain,
                RackAction.job_id.in_(job_ids),
            )
            .order_by(RackAction.id)
            .with_for_update()
        )

    async def note_facts_changed(self, session: AsyncSession, *, job_ids: tuple[str, ...]) -> None:
        """Called after locking the original jobs and admitting a new host fact."""
        await session.execute(
            update(RackJob)
            .where(
                RackJob.owner_domain == self.owner_domain, RackJob.id.in_(job_ids), RackJob.pending
            )
            .values(facts_revision=RackJob.facts_revision + 1)
        )

    async def _actions(self, session, job, *, lock=False):
        query = (
            select(RackAction)
            .where(RackAction.job_id == job.id, RackAction.owner_domain == self.owner_domain)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.order_by(RackAction.id).with_for_update()
        return sorted(await session.scalars(query), key=lambda row: row.ordinal)

    async def register(self, session: AsyncSession, origin: JobOrigin) -> str:
        identity = dict(
            owner_domain=self.owner_domain,
            work_line_id=origin.work_line_id,
            source_kind=origin.source_kind,
            source_ref=origin.source_ref,
            rack_id=origin.rack_id,
        )
        if not all(identity.values()) or not origin.cycle_id:
            raise ValueError("incomplete rack origin")
        values = asdict(origin)
        del values["cycle_id"]
        insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        await session.execute(
            insert(RackJob)
            .values(
                **values,
                id=origin.cycle_id,
                original_cycle_id=origin.cycle_id,
                owner_domain=self.owner_domain,
            )
            .on_conflict_do_nothing(index_elements=list(identity))
        )
        job = (
            await session.scalars(
                self._jobs.filter_by(**identity)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one()
        # A drain candidate can acquire its original approved resources exactly once.
        if (
            job.state == "ACTIVE"
            and job.resources is None
            and origin.resources is not None
            and origin.caller
            and origin.workline_code
            and origin.point
            and job.task_id == origin.task_id
            and all(
                getattr(job, k) in (None, values[k])
                for k in ("caller", "workline_code", "point", "initial_face")
            )
        ):
            for k in ("resources", "caller", "workline_code", "point", "initial_face"):
                setattr(job, k, values[k])
        if job.task_id and job.point and job.resources is not None and job.admission_order is None:
            maximum = await session.scalar(
                select(func.max(RackJob.admission_order)).where(
                    RackJob.work_line_id == job.work_line_id,
                    RackJob.task_id == job.task_id,
                    RackJob.point == job.point,
                )
            )
            job.admission_order = (maximum or 0) + 1
            await session.flush()
        return job.id

    async def progress(self, session: AsyncSession, *, job_id: str) -> RackProgress | None:
        job = await self._job(session, job_id)
        return _snapshot(job, await self._actions(session, job)) if job else None

    async def jobs(
        self,
        session: AsyncSession,
        *,
        work_line_id: int,
        source_kinds: tuple[str, ...],
        task_id: str | None = None,
        source_ref: str | None = None,
        pending: bool | None = None,
    ) -> tuple[RackProgress, ...]:
        query = self._jobs.where(
            RackJob.work_line_id == work_line_id, RackJob.source_kind.in_(source_kinds)
        )
        if source_ref is not None:
            query = query.where(RackJob.source_ref == source_ref)
        if task_id is not None:
            query = query.where(RackJob.task_id == task_id)
        if pending is not None:
            query = query.where(RackJob.pending == pending)
        rows = await session.execute(
            query.add_columns(RackAction)
            .outerjoin(RackAction, RackAction.job_id == RackJob.id)
            .order_by(RackJob.id, RackAction.ordinal)
        )
        grouped = {}
        for job, action in rows:
            _, actions = grouped.setdefault(job.id, (job, []))
            if action is not None:
                actions.append(action)
        return tuple(_snapshot(job, actions) for job, actions in grouped.values())

    def _pending_ids(self, kinds):
        return select(RackJob.id).where(
            RackJob.owner_domain == self.owner_domain,
            RackJob.pending,
            RackJob.source_kind.in_(kinds),
        )

    async def pending_upper_id(
        self, session: AsyncSession, *, source_kinds: tuple[str, ...]
    ) -> str | None:
        return await session.scalar(
            self._pending_ids(source_kinds).order_by(RackJob.id.desc()).limit(1)
        )

    async def pending_ids(
        self,
        session: AsyncSession,
        *,
        source_kinds: tuple[str, ...],
        after_id: str | None,
        upper_id: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        if limit < 1 or upper_id is None:
            return ()
        query = self._pending_ids(source_kinds).where(RackJob.id <= upper_id)
        if after_id is not None:
            query = query.where(RackJob.id > after_id)
        return tuple(await session.scalars(query.order_by(RackJob.id).limit(limit)))

    async def handed_off_position_sources(
        self, session, *, point: str, source_versions: dict[str, int]
    ) -> frozenset[str]:
        if not source_versions:
            return frozenset()
        rows = await session.execute(
            select(RackAction, RackJob)
            .join(RackJob, RackJob.id == RackAction.job_id)
            .where(
                RackAction.id.in_(source_versions),
                RackJob.point == point,
                RackJob.pending.is_(False),
                RackJob.closure_reason == "DEPARTURE_ACK",
            )
        )
        return frozenset(
            action.id
            for action, job in rows
            if action.position_revision == source_versions[action.id]
            and job.closure_basis
            and job.closure_basis.get("action_id") == job.current_action_id
            and job.closure_basis.get("acceptance_basis")
            and job.completed_at_ms is not None
        )

    async def has_position_conflict(self, session: AsyncSession, *, point: str) -> bool:
        holders = await session.scalar(
            select(func.count())
            .select_from(RackJob)
            .where(
                RackJob.pending,
                RackJob.position_action_id.is_not(None),
                RackJob.actual_position["location_code"].as_string() == point,
            )
        )
        return holders > 1 or await self._position_conflict(session, point)

    async def _position_conflict(self, session, point, *, exclude_job_id=None):
        conflict = (
            select(RackAction.id)
            .where(
                RackAction.job_id == RackJob.id,
                (
                    (RackAction.phase == "REVIEW")
                    & (RackAction.actual_position["location_code"].as_string() == point)
                )
                | (
                    RackAction.basis["unconsumed_locations"][point].as_integer()
                    > func.coalesce(RackAction.outcome_revision, 0)
                ),
            )
            .exists()
        )
        query = select(RackJob.id).where(
            RackJob.pending,
            conflict
            | (
                ((RackJob.point != point) | (RackJob.review_reason == "POSITION_CONFLICT"))
                & (RackJob.actual_position["location_code"].as_string() == point)
            ),
        )
        if exclude_job_id is not None:
            query = query.where(RackJob.id != exclude_job_id)
        return bool(await session.scalar(query.limit(1)))

    async def withdraw_unused(
        self, session: AsyncSession, *, job_id: str, source: Coverage
    ) -> bool:
        job = await self._job(session, job_id, lock=True)
        if not job or job.source_kind != "ROLLER_DRAIN":
            return False
        scope = (
            job.owner_domain,
            str(job.work_line_id),
            job.source_kind,
            job.source_ref,
            job.rack_id,
        )
        if not (
            _covered(source, scope)
            and source.scope == scope
            and source.collection == "unused_drain_candidate"
            and source.anchor.kind == "drain_decision"
            and source.anchor.key == ("DRAIN", job.source_ref)
            and not source.rows
        ):
            return False
        if job.state == "WITHDRAWN":
            return True
        if (
            job.state != "ACTIVE"
            or job.position_action_id
            or job.actual_position
            or await self._actions(session, job, lock=True)
        ):
            return False
        _close(job, "WITHDRAWN", "WITHDRAWN", asdict(source), unix_ms(datetime.now(UTC)))
        return True

    @staticmethod
    def has_external_responsibility(job, actions):
        if job.actual_position or job.position_action_id:
            return True
        return any(
            a.transport_task_id
            or not (
                a.kind == "ENTER"
                and a.phase == "REGISTERED"
                and (a.basis or {}).get("registration_at_cutover") == "LOCAL_ONLY"
            )
            for a in actions
        )

    async def admission_jobs(self, session, *, work_line_id):
        """All owners' original jobs; callers acquire this set before host/action locks."""
        return tuple(
            await session.scalars(
                select(RackJob)
                .where(RackJob.work_line_id == work_line_id, RackJob.pending)
                .order_by(RackJob.id)
                .execution_options(populate_existing=True)
            )
        )

    async def lock_admission_jobs(self, session, *, job_ids):
        await session.execute(
            select(RackJob.id).where(RackJob.id.in_(job_ids)).order_by(RackJob.id).with_for_update()
        )

    async def lock_admission_actions(self, session, *, job_ids):
        await session.execute(
            select(RackAction.id)
            .where(RackAction.job_id.in_(job_ids))
            .order_by(RackAction.id)
            .with_for_update()
        )

    async def _entry_available(self, session: AsyncSession, job: RackJob) -> bool:
        line = await session.get(WorkLine, job.work_line_id, populate_existing=True)
        config = await session.scalar(
            select(WorkLinePosition).where(
                WorkLinePosition.work_line_id == job.work_line_id,
                WorkLinePosition.position_code == job.point,
            )
        )
        if not line or not line.is_enabled or line.business_code != self.owner_domain or not config:
            return False
        drain = job.source_kind == "ROLLER_DRAIN"
        if drain != (job.task_id is None):
            return False
        if not drain and (
            not job.task_id
            or not self.task_admission
            or not await self.task_admission.allows_task_entry(
                session, work_line_id=job.work_line_id, task_id=job.task_id
            )
        ):
            return False
        rows = await session.execute(
            select(RackJob, RackAction)
            .outerjoin(RackAction, RackAction.job_id == RackJob.id)
            .where(
                RackJob.pending,
                RackJob.id != job.id,
                or_(RackJob.point == job.point, RackJob.rack_id == job.rack_id),
            )
            .order_by(RackJob.id, RackAction.ordinal)
        )
        grouped = {}
        for other, action in rows:
            _, actions = grouped.setdefault(other.id, (other, []))
            if action is not None:
                actions.append(action)
        occupied = 0
        for other, actions in grouped.values():
            external = self.has_external_responsibility(other, actions)
            if external and other.rack_id == job.rack_id:
                return False
            if other.point != job.point:
                continue
            same = (
                other.work_line_id == job.work_line_id
                and other.owner_domain == job.owner_domain
                and other.task_id == job.task_id
            )
            if external and (drain or not same):
                return False
            if not same:
                continue
            bound = any(a.kind == "ENTER" and a.transport_task_id for a in actions)
            occupied += bool(bound)
            if (
                not drain
                and not bound
                and other.state == "ACTIVE"
                and not other.release_requested
                and other.admission_order is not None
                and job.admission_order is not None
                and other.admission_order < job.admission_order
            ):
                return False
        return drain or (
            job.admission_order is not None
            and (config.capacity is None or occupied < config.capacity)
        )

    async def prepare(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        kind: Literal["ENTER", "ROTATE"],
        source_ref: str,
        face: str | None,
    ) -> str | None:
        if kind not in ("ENTER", "ROTATE") or not source_ref or (kind == "ROTATE" and face is None):
            return None
        job = await self._job(session, job_id, lock=True)
        if not job:
            return None
        actions = await self._actions(session, job, lock=True)
        target = _point(job.point)
        original = next((a for a in actions if a.kind == kind and a.source_ref == source_ref), None)
        if original:
            return (
                original.id
                if original.phase != "WITHDRAWN"
                and original.target == target
                and original.target_face == face
                else None
            )
        if (
            job.state != "ACTIVE"
            or job.release_requested
            or not job.point
            or not job.workline_code
            or not job.caller
            or job.resources is None
            or await self.has_position_conflict(session, point=job.point)
        ):
            return None
        if actions:
            last = actions[-1]
            applied = await self._applied(session, job, actions)
            if last.phase == "REGISTERED" and job.review_reason == "AWAITING_ORIGINAL_RESULT":
                return (
                    last.id
                    if (last.kind, last.target, last.target_face) == (kind, target, face)
                    else None
                )
            if kind != "ROTATE" or not applied:
                return None
            if (
                not last.outcome
                or last.outcome["status"] != "SUCCEEDED"
                or job.actual_position != target
            ):
                return None
        elif kind != "ENTER":
            return None
        if kind == "ENTER" and not await self._entry_available(session, job):
            return None
        action = await self._move(session, job, actions, kind, source_ref, target, face)
        return action.id

    async def _move(self, session, job, actions, kind, source_ref, target, face):
        manual = job.source_kind.startswith("MANUAL_")
        template = {
            "ENTER": "F01" if manual else "CTU01",
            "ROTATE": "CK04" if manual else "CTU02",
            "DEPART": "F01" if manual else "CTU03",
        }[kind]
        source = (
            {"kind": "RACK", "location_code": job.rack_id} if kind == "ENTER" else _point(job.point)
        )
        action = RackAction(
            id=_id(),
            job_id=job.id,
            owner_domain=job.owner_domain,
            work_line_id=job.work_line_id,
            ordinal=1 + max((a.ordinal for a in actions), default=0),
            kind=kind,
            source_ref=source_ref,
            completion_rule="ACCEPTED_DEPARTURE" if kind == "DEPART" else "APPLIED_RESULT",
            previous_action_id=actions[-1].id if actions else None,
            previous_decision_phase="READY" if kind == "DEPART" else None,
            phase="REGISTERED",
            caller=job.caller,
            source=source,
            target=target,
            target_face=face,
            template=template,
            client_request_id=_id(),
        )
        await self._bind_move(session, job, action)
        job.current_action_id = action.id
        session.add(action)
        await session.flush()
        actions.append(action)
        return action

    async def _bind_move(self, session, job, action):
        caller = _caller(action)
        args = dict(
            client_request_id=action.client_request_id,
            caller=caller,
            rack_id=job.rack_id,
            cycle_id=job.original_cycle_id,
            rcs_template_id=action.template,
            target_face=action.target_face,
        )
        transport = self.capabilities.transport
        if action.kind == "ROTATE":
            handle = await transport.rotate_rack(
                session, **args, position=RackPosition(action.source["location_code"])
            )
        else:
            handle = await transport.move_rack(
                session,
                **args,
                source=(Rack if action.kind == "ENTER" else RackPosition)(
                    action.source["location_code"]
                ),
                target=(Zone if action.target["kind"] == "ZONE" else RackPosition)(
                    action.target["location_code"]
                ),
            )
        view = await transport.get_result(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=handle.client_request_id,
            caller=caller,
        )
        if (
            view.availability != "FOUND"
            or view.owner_domain != job.owner_domain
            or view.cycle_id != job.original_cycle_id
            or not view.operation_id
            or not view.submission_message_id
            or not view.submission_target
        ):
            raise ValueError("registered transport identity unavailable")
        action.transport_task_id = handle.transport_task_id
        action.submit_operation_id = view.operation_id
        action.submit_message_id = view.submission_message_id
        action.submit_target = view.submission_target

    async def _result(self, session, job, action):
        if not action.transport_task_id or not action.client_request_id or not action.caller:
            return None
        caller = _caller(action)
        result = await self.capabilities.transport.get_result(
            session,
            transport_task_id=action.transport_task_id,
            client_request_id=action.client_request_id,
            caller=caller,
        )
        if (
            result.availability != "FOUND"
            or result.owner_domain != job.owner_domain
            or result.cycle_id != job.original_cycle_id
            or result.operation_id != action.submit_operation_id
            or result.client_request_id != action.client_request_id
            or result.transport_task_id != action.transport_task_id
            or result.workline_id != caller.workline_id
            or result.station_id != caller.station_id
            or result.kind != ("RACK_ROTATE" if action.kind == "ROTATE" else "RACK_MOVE")
        ):
            return None
        return result

    async def final_result(
        self, session: AsyncSession, *, job_id: str, action_id: str
    ) -> TransportTaskResult | None:
        job = await self._job(session, job_id)
        if job:
            action = await session.scalar(
                select(RackAction).where(RackAction.id == action_id, RackAction.job_id == job_id)
            )
            if action:
                return await self._result(session, job, action)
        return None

    async def _reject_unarrived(self, session, job, actions, demand, handoff, now_ms):
        if (
            len(actions) != 1
            or actions[0].kind != "ENTER"
            or job.actual_position
            or job.position_action_id
        ):
            return False
        if not (
            _at_point(demand, job)
            and demand.state in ("NONE", "CONTINUE")
            and _at_point(handoff, job)
            and handoff.state == "CLEAR"
        ):
            return False
        action = actions[0]
        result = await self._result(session, job, action)
        if not (
            result
            and result.closure == "CLOSED"
            and result.closure_reason == "SUBMIT_REJECTION"
            and result.submission_state == "AVAILABLE"
            and result.submission_response_state == "VALID"
            and result.submission_http_status in (409, 422)
            and not result.submission_delivery_uncertain
            and not result.has_pending_outcome
            and result.outcome is None
            and result.position_observation is None
            and result.submission_message_id == action.submit_message_id
            and result.submission_target == action.submit_target
        ):
            return False
        predecessors = await self.capabilities.transport.list_unclosed_predecessors(
            session,
            caller=_caller(job),
            cycle_id=job.original_cycle_id,
            predecessor_ids=(action.transport_task_id,),
            rack_id=job.rack_id,
        )
        if (
            not predecessors.complete
            or predecessors.unresolved
            or predecessors.caller != _caller(job)
            or predecessors.cycle_id != job.original_cycle_id
            or predecessors.rack_id != job.rack_id
            or predecessors.container_id is not None
            or predecessors.requested_ids != (action.transport_task_id,)
            or [(item.transport_task_id, item.client_request_id) for item in predecessors.items]
            != [(action.transport_task_id, action.client_request_id)]
        ):
            return False
        _close(
            job,
            "NOT_ARRIVED",
            "NOT_ARRIVED",
            dict(
                action_id=action.id,
                submission_rejection=result.closure_basis,
                demand=asdict(demand),
                handoff=asdict(handoff),
            ),
            now_ms,
        )
        return True

    async def _applied(self, session, job, actions):
        """Refresh only original moves. Older unresolved observations still protect the line."""
        moves = [a for a in actions if a.kind in ("ENTER", "ROTATE") and a.phase != "WITHDRAWN"]
        if not moves:
            return False
        reason = None
        for action in moves:
            result = await self._result(session, job, action)
            if not result:
                action.phase = "REVIEW"
                reason = "ORIGINAL_RESULT_UNAVAILABLE"
                continue
            outcome, observation = result.outcome, result.position_observation
            if any(
                v["rack_id"] != job.rack_id
                or v["transport_task_id"] != action.transport_task_id
                or v["kind"] != result.kind
                for v in (outcome, observation)
                if v
            ):
                reason = "ORIGINAL_RESULT_MISMATCH"
                action.phase = "REVIEW"
                continue
            if outcome is not None and (
                action.outcome_revision is None
                or result.outcome_revision >= action.outcome_revision
            ):
                action.outcome, action.outcome_revision = outcome, result.outcome_revision
            if observation is not None and (
                action.observation_revision is None
                or observation["outcome_revision"] >= action.observation_revision
            ):
                action.position_observation, action.observation_revision = (
                    observation,
                    observation["outcome_revision"],
                )
            action.pending_revision = result.pending_revision
            newer_observation = (
                action.observation_revision is not None
                and action.observation_revision > (result.outcome_revision or 0)
            )
            actual = action.position_observation if newer_observation else outcome
            if actual:
                action.actual_position, action.actual_face = (
                    actual["final_position"],
                    actual["arrival_face"],
                )
                action.position_revision = actual["outcome_revision"]
            if actual and actual["final_position"] and (action is moves[-1] or newer_observation):
                job.actual_position, job.actual_face = (
                    actual["final_position"],
                    actual["arrival_face"],
                )
                job.position_action_id = action.id
            if newer_observation:
                action.phase, reason = "REVIEW", "POSITION_CONFLICT"
            elif outcome is None:
                reason = reason or "AWAITING_ORIGINAL_RESULT"
            elif (
                outcome["position_unknown"]
                or not outcome["final_position"]
                or result.has_pending_outcome
                or result.closure != "CLOSED"
                or result.outcome_revision != action.outcome_revision
            ):
                action.phase = "REVIEW"
                reason = reason or "UNRESOLVED_EXECUTION"
            elif outcome["status"] == "SUCCEEDED" and (
                outcome["final_position"] != action.target
                or (
                    action.target_face is not None and outcome["arrival_face"] != action.target_face
                )
            ):
                action.phase, reason = "REVIEW", "POSITION_CONFLICT"
            else:
                action.phase = "APPLIED"
        if reason is None:
            ids = tuple(a.transport_task_id for a in moves)
            for start in range(0, len(ids), 100):
                result = await self.capabilities.transport.list_unclosed_predecessors(
                    session,
                    caller=_caller(job),
                    cycle_id=job.original_cycle_id,
                    predecessor_ids=ids[start : start + 100],
                    rack_id=job.rack_id,
                )
                chunk = moves[start : start + 100]
                if (
                    not result.complete
                    or result.unresolved
                    or result.caller != _caller(job)
                    or result.cycle_id != job.original_cycle_id
                    or result.rack_id != job.rack_id
                    or result.container_id is not None
                    or result.requested_ids != ids[start : start + 100]
                    or [
                        (item.transport_task_id, item.client_request_id, item.outcome_revision)
                        for item in result.items
                    ]
                    != [
                        (a.transport_task_id, a.client_request_id, a.outcome_revision)
                        for a in chunk
                    ]
                ):
                    reason = "UNRESOLVED_PREDECESSOR"
                    break
        # Consuming an applied non-success elsewhere does not release that point's protection.
        external = job.actual_position is not None and job.actual_position != _point(job.point)
        job.review_reason = reason or ("POSITION_CONFLICT" if external else None)
        return reason is None

    async def advance(
        self,
        session: AsyncSession,
        *,
        job_id: str,
        demand: PointDemand,
        handoff: DirectHandoff,
        now_ms: int,
        authorized_original_id: str | None = None,
    ) -> RackProgress:
        job = await self._job(session, job_id, lock=True)
        if not job:
            raise LookupError("unknown owner job")
        actions = await self._actions(session, job, lock=True)
        if not job.pending:
            return _snapshot(job, actions)
        if await self._reject_unarrived(session, job, actions, demand, handoff, now_ms):
            return _snapshot(job, actions)
        original = actions[-1] if actions else None
        unbound = (
            original
            and original.kind in ("ENTER", "ROTATE", "DEPART")
            and original.phase == "REGISTERED"
            and original.transport_task_id is None
        )
        if unbound and (original.basis or {}).get("registration_at_cutover") != "LOCAL_ONLY":
            job.review_reason = "ORIGINAL_REGISTRATION_UNPROVED"
            return _snapshot(job, actions)
        if original and original.kind == "DEPART" and not unbound:
            await self._accept(session, job, actions, now_ms)
            return _snapshot(job, actions)
        predecessors = actions[:-1] if unbound else actions
        position = (
            next((a for a in predecessors if a.id == job.position_action_id), None)
            if unbound
            else None
        )
        original_position_revision = position.position_revision if position else None
        applied = await self._applied(session, job, predecessors)
        previous = predecessors[-1] if predecessors else None
        if previous and previous.kind == "DEPARTURE_DECIDE":
            if not await self._consume_decision(session, job, previous, now_ms):
                return _snapshot(job, actions)
        known_demand = isinstance(demand, PointDemand) and _at_point(demand, job)
        none = known_demand and demand.state == "NONE"
        clear = (
            isinstance(handoff, DirectHandoff)
            and _at_point(handoff, job)
            and handoff.state == "CLEAR"
        )
        if none and job.state == "ACTIVE":
            job.release_requested = True
            job.release_basis = _json(asdict(demand))
        elif known_demand and demand.state == "CONTINUE" and job.state == "ACTIVE":
            job.release_requested = False
        authorized = (
            unbound
            and authorized_original_id == original.id
            and known_demand
            and clear
            and not await self.has_position_conflict(session, point=job.point)
        )
        if not actions or (unbound and original.kind == "ENTER" and not predecessors):
            if none and clear and not job.position_action_id and not job.actual_position:
                _close(
                    job,
                    "WITHDRAWN",
                    "WITHDRAWN",
                    dict(
                        demand=asdict(demand),
                        handoff=asdict(handoff),
                        **(
                            {
                                "action_id": original.id,
                                "client_request_id": original.client_request_id,
                                "registration_at_cutover": "LOCAL_ONLY",
                            }
                            if unbound
                            else {}
                        ),
                    ),
                    now_ms,
                )
            elif (
                authorized
                and demand.state == "CONTINUE"
                and job.state == "ACTIVE"
                and await self._entry_available(session, job)
            ):
                await self._bind_move(session, job, original)
            return _snapshot(job, actions)
        if unbound:
            if not (
                applied
                and position
                and position.id == job.position_action_id
                and position.position_revision == original_position_revision
                and position.outcome
                and job.actual_position == _point(job.point)
            ):
                return _snapshot(job, actions)
            if original.kind == "ROTATE" and job.state == "ACTIVE":
                if none and clear:
                    if await self.has_position_conflict(session, point=job.point):
                        job.review_reason = "OTHER_POSITION_CONFLICT"
                        return _snapshot(job, actions)
                    original.phase = "WITHDRAWN"
                    original.basis = {
                        **original.basis,
                        "withdrawn_at_ms": now_ms,
                        "withdrawal_reason": "NO_FACE_DEMAND",
                        "demand": _json(asdict(demand)),
                        "handoff": _json(asdict(handoff)),
                    }
                else:
                    if (
                        authorized
                        and demand.state == "CONTINUE"
                        and position.outcome["status"] == "SUCCEEDED"
                        and job.actual_face != original.target_face
                    ):
                        await self._bind_move(session, job, original)
                    return _snapshot(job, actions)
            elif not (
                authorized
                and original.kind == "DEPART"
                and job.state == "RELEASING"
                and (none or position.outcome["status"] == "SUCCEEDED")
                and previous
                and previous.kind == "DEPARTURE_DECIDE"
                and previous.phase == "READY"
                and previous.id == original.previous_action_id
            ):
                return _snapshot(job, actions)
        if not applied:
            return _snapshot(job, actions)
        actual = job.actual_position
        if actual != _point(job.point):
            location = actual["location_code"] if actual else None
            bindings = await session.scalars(select(WorkLine.position_bindings))
            frozen = await session.scalars(select(RackJob.resources).where(RackJob.pending))
            managed = location and (
                any(location in ((r or {}).get("positions") or {}).values() for r in frozen)
                or any(location in b.values() for b in bindings)
                or await session.scalar(
                    select(RackJob.id).where(RackJob.pending, RackJob.point == location).limit(1)
                )
                or await self._position_conflict(session, location, exclude_job_id=job.id)
            )
            move = actions[-1]
            if (
                location
                and not managed
                and move.kind == "ENTER"
                and move.outcome["status"] in ("FAILED", "CANCELLED")
                and known_demand
                and demand.state in ("CONTINUE", "NONE")
                and clear
            ):
                _close(
                    job,
                    "NOT_ARRIVED",
                    "NOT_ARRIVED",
                    dict(
                        action_id=move.id,
                        outcome=move.outcome,
                        demand=asdict(demand),
                        handoff=asdict(handoff),
                    ),
                    now_ms,
                )
            else:
                job.review_reason = "POSITION_CONFLICT"
        elif (
            clear
            and known_demand
            and demand.state in ("NONE", "CONTINUE")
            and (none or job.state == "RELEASING")
        ):
            if await self.has_position_conflict(session, point=job.point):
                job.review_reason = "OTHER_POSITION_CONFLICT"
            else:
                await self._departure(session, job, actions, demand, handoff, now_ms)
        await session.flush()
        return _snapshot(job, actions)

    async def _consume_decision(self, session, job, previous, now_ms):
        """Read the original response once, including when new movement is blocked."""
        from app.businesses._components.rack_dispatch.request.rack_departure_decide_v1 import (
            DECISION,
            OPERATION,
            DepartureRequest,
        )

        stored = previous.decision
        if (
            previous.operation != OPERATION
            or not previous.decision_operation_id
            or previous.message_id is None
            or previous.message_id <= 0
        ):
            job.review_reason = "INVALID_DEPARTURE_IDENTITY"
            return False
        try:
            original = DepartureRequest.model_validate(previous.request)
        except ValueError:
            job.review_reason = "INVALID_DEPARTURE_REQUEST"
            return False
        if (
            original.rack_id != job.rack_id
            or original.workline_code != job.workline_code
            or original.current_location.location_code != job.point
        ):
            job.review_reason = "INVALID_DEPARTURE_REQUEST"
            return False
        if stored is None:
            result = await self.capabilities.wms.get_result(
                session,
                message_id=previous.message_id,
                operation=previous.operation,
                operation_id=previous.decision_operation_id,
            )
            if (
                result.availability != "FOUND"
                or result.response_state != "VALID"
                or (result.message_id, result.operation, result.operation_id)
                != (previous.message_id, OPERATION, previous.decision_operation_id)
                or (result.http_status, result.response_code) != (200, "DECIDED")
            ):
                return False
            stored = result.response_data
        try:
            decision = DECISION.validate_python(stored).model_dump(mode="json")
        except ValueError:
            job.review_reason = "INVALID_DEPARTURE_DECISION"
            return False
        if decision.get("rack_destination") == previous.request["current_location"]:
            job.review_reason = "INVALID_DEPARTURE_DESTINATION"
            return False
        if previous.decision is None:
            previous.phase = decision["result"]
            previous.decision = decision
            previous.decision_consumed_at_ms = now_ms
            if previous.phase == "WAIT":
                previous.due_at_ms = now_ms + decision["retry_after_ms"]
        return previous.phase == decision["result"]

    async def _departure(self, session, job, actions, demand, handoff, now_ms):
        from app.businesses._components.rack_dispatch.request.rack_departure_decide_v1 import (
            OPERATION,
            DepartureRequest,
        )

        last = actions[-1]
        predecessor = actions[-2] if last.kind == "DEPART" else last
        previous = predecessor if predecessor.kind == "DEPARTURE_DECIDE" else None
        position = next(a for a in actions if a.id == job.position_action_id)
        # JSON-normalized original facts also allow a stable comparison after a database round trip.
        basis = _json(
            dict(
                demand=asdict(demand),
                handoff=asdict(handoff),
                position_action_id=position.id,
                outcome_revision=position.outcome_revision,
                position=job.actual_position,
                face=job.actual_face,
            )
        )
        statuses = {
            fact.version[0]
            for coverage in demand.coverage
            for fact in (coverage.anchor, *coverage.rows)
            if fact.kind == "task_status" and fact.key == (job.task_id,) and len(fact.version) == 1
        }
        basis["task_status"] = next(iter(statuses)) if len(statuses) == 1 else None
        if previous:
            if previous.phase == "READY":
                destination = previous.decision["rack_destination"]
                target = {
                    "kind": destination["type"],
                    "location_code": destination["location_code"],
                }
                if last.kind == "DEPART":
                    if last.target == target:
                        await self._bind_move(session, job, last)
                    else:
                        job.review_reason = "INVALID_DEPARTURE_INTENT"
                else:
                    await self._move(
                        session,
                        job,
                        actions,
                        "DEPART",
                        previous.decision_operation_id,
                        target,
                        None,
                    )
                return
            if now_ms < previous.due_at_ms:
                before = previous.basis or {}
                position_changed = all(
                    before.get(key) is not None
                    for key in ("position_action_id", "outcome_revision")
                ) and any(
                    basis[key] != before[key] for key in ("position_action_id", "outcome_revision")
                )
                status_changed = bool(
                    before.get("task_status")
                    and basis["task_status"]
                    and before["task_status"] != basis["task_status"]
                )
                if not job.source_kind.startswith("MANUAL_") or not (
                    position_changed or status_changed
                ):
                    return
        payload = DepartureRequest(
            workline_code=job.workline_code,
            rack_id=job.rack_id,
            current_location={"type": "RACK_POSITION", "location_code": job.point},
            current_face=job.actual_face,
        )
        op_id = _id()
        message = await self.capabilities.wms.send(session, payload, operation_id=op_id)
        row = RackAction(
            id=_id(),
            job_id=job.id,
            owner_domain=job.owner_domain,
            work_line_id=job.work_line_id,
            ordinal=last.ordinal + 1,
            kind="DEPARTURE_DECIDE",
            source_ref=previous.id if previous else position.id,
            phase="REGISTERED",
            previous_action_id=previous.id if previous else None,
            previous_decision_phase="WAIT" if previous else None,
            caller=job.caller,
            source=job.actual_position,
            operation=OPERATION,
            decision_operation_id=op_id,
            message_id=message.message_id,
            request=payload.model_dump(mode="json"),
            basis=basis,
        )
        job.current_action_id = row.id
        session.add(row)
        actions.append(row)
        job.state = "RELEASING"
        if not previous:
            job.release_basis = basis

    async def _accept(self, session, job, actions, now_ms):
        action = actions[-1]
        ready = next((a for a in actions if a.id == action.previous_action_id), None)
        if (
            job.state != "RELEASING"
            or action.completion_rule != "ACCEPTED_DEPARTURE"
            or not ready
            or ready.kind != "DEPARTURE_DECIDE"
            or ready.phase != "READY"
            or not ready.decision
            or action.template != ("F01" if job.source_kind.startswith("MANUAL_") else "CTU03")
            or action.source != _point(job.point)
            or action.target_face is not None
            or action.caller != job.caller
        ):
            job.review_reason = "INVALID_DEPARTURE_INTENT"
            return
        if not await self._consume_decision(session, job, ready, now_ms):
            return
        destination = ready.decision["rack_destination"]
        if action.target != {
            "kind": destination["type"],
            "location_code": destination["location_code"],
        }:
            job.review_reason = "INVALID_DEPARTURE_INTENT"
            return
        acceptance = await self.capabilities.transport.get_submission_acceptance(
            session,
            transport_task_id=action.transport_task_id,
            client_request_id=action.client_request_id,
            caller=_caller(action),
        )
        basis = acceptance.basis
        if (
            acceptance.state != "ACCEPTED"
            or basis is None
            or basis.owner_domain != job.owner_domain
            or basis.cycle_id != job.original_cycle_id
            or basis.caller != _caller(action)
            or (basis.kind, basis.operation) != ("RACK_MOVE", TRANSPORT_OPERATION)
            or basis.transport_task_id != action.transport_task_id
            or basis.business_key != action.transport_task_id
            or basis.client_request_id != action.client_request_id
            or basis.operation_id != action.submit_operation_id
            or basis.message_id != action.submit_message_id
            or basis.target != action.submit_target
            or (basis.http_status, basis.response_code)
            not in ((202, "RECEIVED"), (200, "DUPLICATE"))
            or basis.frozen
            != {
                "kind": "RACK_MOVE",
                "rack_id": job.rack_id,
                "rcs_template_id": action.template,
                "source": action.source,
                "target": action.target,
            }
        ):
            return
        action.phase, action.acceptance_basis, action.accepted_at_ms = (
            "ACCEPTED",
            asdict(basis),
            basis.accepted_at,
        )
        _close(
            job,
            "COMPLETED",
            "DEPARTURE_ACK",
            dict(action_id=action.id, acceptance_basis=asdict(basis)),
            now_ms,
        )
        await session.flush()
