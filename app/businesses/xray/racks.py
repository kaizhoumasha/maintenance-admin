"""X-Ray point intent and handoff. RackDispatch owns every rack action."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import select

from app.businesses._components.rack_dispatch.contracts import (
    Coverage,
    DirectHandoff,
    FactRef,
    JobOrigin,
    PointDemand,
    PointRef,
)
from app.businesses._components.rack_dispatch.service import XRAY_SOURCES, RackDispatch
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.wms_transport import TransportCaller
from app.models import WorkLinePosition

from .contracts import Face, Identifier, PositiveInt, RackTarget, Strict
from .definition import DEFINITION
from .models import RackPreparation, Reel, ReelOperation

OWNER = DEFINITION.code
PREPARATIONS = {"REPLENISH_REQUIRED", "ROTATE_REQUIRED", "REPLACE_REQUIRED", "DEPART_REQUIRED"}


class Slot(Strict):
    rack_layer: Annotated[int, Field(ge=1, le=5)]
    rack_column: PositiveInt


class RackPoint(Strict):
    location_code: Identifier
    location_id: Identifier
    templates: dict[Literal["ENTER", "ROTATE", "DEPART"], Identifier] = Field(default_factory=dict)
    slots: dict[Face, dict[Identifier, Slot]]
    fixed_test_rack: Identifier | None = None

    @model_validator(mode="after")
    def complete_mapping(self) -> Self:
        if self.fixed_test_rack:
            if self.templates or len(self.slots) != 1:
                raise ValueError(
                    "Fixed X-Ray test rack requires one face and no transport templates"
                )
        else:
            if set(self.templates) != {"ENTER", "ROTATE", "DEPART"} or set(self.slots) != {
                "A",
                "B",
            }:
                raise ValueError("X-Ray rack requires three templates and both faces")
            if self.templates["DEPART"] not in {"F01", "CTU03"}:
                raise ValueError("X-Ray departure needs a template with the shared acceptance rule")
        for slots in self.slots.values():
            positions = {(slot.rack_layer, slot.rack_column) for slot in slots.values()}
            if not slots or len(positions) != len(slots):
                raise ValueError("X-Ray rack slots must identify distinct positions on each face")
        return self


@dataclass(frozen=True)
class PointState:
    config: RackPoint
    binding: dict
    latest: RackPreparation | None

    @property
    def request_basis(self):
        return {
            "binding": self.binding,
            "preparation": self.latest.id if self.latest else None,
            "preparing": self.latest.pending if self.latest else False,
        }


class XrayRacks:
    def __init__(self, dispatch: RackDispatch, configs: dict[str, RackPoint], mock_targets=None):
        self.dispatch = dispatch
        self.configs = configs
        self.mock_targets = mock_targets or {}
        if any(
            config.fixed_test_rack and line not in self.mock_targets
            for line, config in configs.items()
        ):
            raise ValueError("Fixed X-Ray test racks require the local X-Ray WMS mock")

    async def lock_point(self, session, line, arm):
        if not line or not line.is_enabled or line.business_code != OWNER:
            return None
        config = self.configs.get(line.line_code)
        if not config or config.location_code != line.position_bindings.get("RETURN_RACK"):
            return None
        point = await session.scalar(
            select(WorkLinePosition)
            .where(
                WorkLinePosition.work_line_id == line.id,
                WorkLinePosition.position_code == config.location_code,
            )
            .with_for_update()
        )
        if not point:
            return None
        jobs = await self.dispatch.admission_jobs(session, work_line_id=line.id)
        await self.dispatch.lock_admission_jobs(session, job_ids=tuple(job.id for job in jobs))
        latest = await session.scalar(
            select(RackPreparation)
            .where(
                RackPreparation.work_line_id == line.id,
                RackPreparation.point == config.location_code,
            )
            .order_by(RackPreparation.id.desc())
            .limit(1)
            .with_for_update()
        )
        binding = {
            "line_version": line.version,
            "point_id": point.id,
            "arm": [arm.id, arm.version] if arm else None,
            "config": canonical_digest(config.model_dump(mode="json", exclude_none=True)),
        }
        return PointState(config, binding, latest)

    async def _handoffs(self, session, point):
        return tuple(
            await session.scalars(
                select(Reel.id)
                .where(
                    Reel.arm_device_code.is_not(None),
                    Reel.actions["OUTLET"]["rack_point"].as_string() == point,
                )
                .order_by(Reel.id)
            )
        )

    async def observe(self, session, job, now_ms, *, prep=None, allow_departure=False):
        point = job.origin.point
        occupied = await self._handoffs(session, point)
        anchor = (
            FactRef("put_target_decision", (prep.operation_id,), (str(prep.id),))
            if prep
            else FactRef("rack_job", (job.id,), (str(job.facts_revision),))
        )
        ref = PointRef(OWNER, job.origin.work_line_id, job.id, job.origin.rack_id, point)
        coverage = Coverage(
            "xray_return_rack",
            (OWNER, str(ref.work_line_id), ref.job_id, ref.rack_id, ref.point),
            anchor,
            tuple(FactRef("rack_put", (key,), ("UNSETTLED",)) for key in occupied),
        )
        return await self.dispatch.advance(
            session,
            job_id=job.id,
            now_ms=now_ms,
            demand=PointDemand(
                ref, "NONE" if allow_departure and not occupied else "CONTINUE", (coverage,)
            ),
            handoff=DirectHandoff(ref, "BLOCKED" if occupied else "CLEAR", (coverage,)),
            allow_new_departure=allow_departure,
        )

    async def _ready(self, session, line_id, point, now_ms):
        jobs = await self.dispatch.jobs(
            session, work_line_id=line_id, source_kinds=XRAY_SOURCES, point=point, pending=True
        )
        if len(jobs) != 1:
            return None
        job = await self.observe(session, jobs[0], now_ms)
        if await self.dispatch.has_conflicting_responsibility(
            session, point=point, rack_id=job.origin.rack_id, exclude_job_id=job.id
        ):
            return None
        ready = await self.dispatch.get_point_preparations(
            session, work_line_id=line_id, point=point, source_kinds=XRAY_SOURCES
        )
        return ready[0] if len(ready) == 1 else None

    async def target(self, session, reel, point, intent, now_ms, *, barcode):
        """Return a rack target, a reason to wait, or the need for a fresh WMS decision."""
        if point is None:
            return "RACK_POINT_UNCONFIGURED", None
        basis = intent.rack_basis
        if basis is None or basis["binding"] != point.binding:
            return "RACK_CONFIGURATION_CHANGED", None
        plan = intent.response
        location = plan["target_locator"] if plan["result"] == "ACCEPT" else plan
        if location["location_code"] != point.config.location_code:
            return "RACK_POINT_MISMATCH", None
        latest = point.latest
        if latest and latest.pending:
            if plan["result"] != "ACCEPT":
                intent.rack_preparation_id = latest.id
            return "RACK_PREPARATION_PENDING", None
        if plan["result"] != "ACCEPT" and basis != point.request_basis:
            return "RACK_REDECIDE", None

        code = point.config.location_code
        fixed = point.config.fixed_test_rack
        if fixed:
            if (
                plan["result"] != "ACCEPT"
                or location["rack_id"] != fixed
                or plan.get("target_preparation") is not None
                or plan != self.mock_targets[reel.workline_code].get(barcode)
            ):
                return "FIXED_TEST_RACK_TARGET_MISMATCH", None
            if await self.dispatch.has_conflicting_responsibility(
                session, point=code, rack_id=fixed
            ) or await self.dispatch.has_position_conflict(session, point=code):
                return "RACK_POSITION_CONFLICT", None
            job = None
        elif plan["result"] == "ACCEPT":
            return await self._authorized_target(session, reel, point, intent, now_ms)
        elif plan["result"] == "REPLENISH_REQUIRED":
            rack_id = plan["incoming_rack"]["rack_id"]
            existing = await self.dispatch.jobs(
                session,
                work_line_id=reel.work_line_id,
                source_kinds=XRAY_SOURCES,
                point=code,
                pending=True,
            )
            if (
                existing
                or await self.dispatch.has_conflicting_responsibility(
                    session, point=code, rack_id=rack_id
                )
                or await self.dispatch.has_position_conflict(session, point=code)
            ):
                return "RACK_POSITION_CONFLICT", None
            job = None
        else:
            job = await self._ready(session, reel.work_line_id, code, now_ms)
            expected = (
                location["rack_id"]
                if plan["result"] == "ACCEPT"
                else plan["current_rack"]["rack_id"]
            )
            if not job or job.origin.rack_id != expected:
                return "RACK_READINESS_REQUIRED", None

        if plan["result"] == "ACCEPT":
            return self._put_target(point, plan, job)
        if plan["result"] == "ROTATE_REQUIRED" and job.actual_face == plan["target_face"]:
            return "RACK_FACE_ALREADY_CURRENT", None
        return await self._prepare_target(session, reel, point, intent, now_ms, job)

    async def _authorized_target(self, session, reel, point, intent, now_ms):
        plan = intent.response
        location = plan["target_locator"]
        rack, face = location["rack_id"], location["rack_face"]
        code = point.config.location_code
        job = await self._ready(session, reel.work_line_id, code, now_ms)
        if job and (job.origin.rack_id, job.actual_face) == (rack, face):
            return self._put_target(point, plan, job)
        # A completed preparation cannot be repeated from the same allocation.
        if intent.rack_preparation_id is not None:
            return "RACK_READINESS_REQUIRED", None
        mode = plan.get("target_preparation")
        if job:
            same_rack = job.origin.rack_id == rack
            if (same_rack and mode != "ROTATE") or (not same_rack and mode != "REPLACE"):
                return "RACK_READINESS_REQUIRED", None
            if await self._authorized_demand(session, reel, point.binding, job):
                return "RACK_PREPARATION_PENDING", None
        elif mode == "ROTATE":
            return "RACK_READINESS_REQUIRED", None
        elif mode is None:
            if not await self.dispatch.point_vacant_for_entry(session, point=code, rack_id=rack):
                return "RACK_READINESS_REQUIRED", None
        else:
            jobs = await self.dispatch.jobs(
                session,
                work_line_id=reel.work_line_id,
                point=code,
                pending=True,
                source_kinds=XRAY_SOURCES,
            )
            if (
                jobs
                or await self.dispatch.has_conflicting_responsibility(
                    session, point=code, rack_id=rack
                )
                or await self.dispatch.has_position_conflict(session, point=code)
            ):
                return "RACK_POSITION_CONFLICT", None
        if self._slot(point, location) is None:
            return "RACK_SLOT_MISMATCH", None
        return await self._prepare_target(session, reel, point, intent, now_ms, job)

    @staticmethod
    async def _authorized_demand(session, reel, binding, job):
        target = ReelOperation.response["target_locator"]
        bases = await session.scalars(
            select(ReelOperation.rack_basis)
            .join(Reel, ReelOperation.reel_key == Reel.id)
            .where(
                Reel.work_line_id == reel.work_line_id,
                Reel.id != reel.id,
                Reel.pending,
                Reel.destination["type"].as_string().is_(None),
                ReelOperation.stage == "OUTLET",
                ReelOperation.response["result"].as_string() == "ACCEPT",
                target["type"].as_string() == "RACK_SLOT",
                target["location_code"].as_string() == job.origin.point,
                target["rack_id"].as_string() == job.origin.rack_id,
                target["rack_face"].as_string() == job.actual_face,
            )
        )
        return any((basis or {}).get("binding") == binding for basis in bases)

    @staticmethod
    def _slot(point, location):
        slot = point.config.slots.get(location["rack_face"], {}).get(location["slot_id"])
        if slot and (slot.rack_layer, slot.rack_column) == (
            location["rack_layer"],
            location["rack_column"],
        ):
            return slot
        return None

    def _put_target(self, point, plan, job):
        location = plan["target_locator"]
        if self._slot(point, location) is None:
            return "RACK_SLOT_MISMATCH", None
        target = RackTarget(
            location_id=point.config.location_id,
            location_type="RETURN_RACK",
            **{
                key: value
                for key, value in location.items()
                if key not in {"type", "location_code"}
            },
            reel_diameter=plan["reel_diameter"],
            reel_thickness=plan["reel_thickness"],
        )
        context = {"rack_locator": location, "rack_point": point.config.location_code}
        if job is not None:
            action = job.actions[-1]
            context.update(
                rack_job_id=job.id,
                rack_action_id=action.id,
                rack_position_revision=action.position_revision,
            )
        return "READY", (target.model_dump(mode="json"), context)

    async def _prepare_target(self, session, reel, point, intent, now_ms, job):
        if job and job.origin.resources.get("templates") != point.config.templates:
            return "RACK_CONFIGURATION_CHANGED", None
        prep = RackPreparation(
            work_line_id=reel.work_line_id,
            point=point.config.location_code,
            operation_id=intent.operation_id,
            old_job_id=job.id if job else None,
        )
        session.add(prep)
        await session.flush()
        intent.rack_preparation_id = prep.id
        await self.advance(session, prep, PointState(point.config, point.binding, prep), now_ms)
        return "RACK_PREPARATION_PENDING", None

    async def advance(self, session, prep, point, now_ms):
        prep.next_check_at_ms = 0
        intent = await session.get(ReelOperation, prep.operation_id)
        reel = await session.get(Reel, intent.reel_key) if intent else None
        if not reel or not intent.rack_basis or not intent.response:
            return "RACK_PREPARATION_EVIDENCE_MISSING"
        plan = intent.response
        mode = plan["result"]
        implicit_entry = mode == "ACCEPT" and plan.get("target_preparation") is None
        old = (
            await self.dispatch.progress(session, job_id=prep.old_job_id)
            if prep.old_job_id
            else None
        )
        if mode == "ACCEPT":
            location = plan["target_locator"]
            if location["type"] != "RACK_SLOT" or location["location_code"] != prep.point:
                return "RACK_PREPARATION_EVIDENCE_MISSING"
            rack, face = location["rack_id"], location["rack_face"]
            hint = plan.get("target_preparation")
            if hint == "ROTATE":
                mode = "ROTATE_REQUIRED"
                expected_old = rack
            elif hint == "REPLACE" and prep.old_job_id:
                mode = "REPLACE_REQUIRED"
                expected_old = old.origin.rack_id if old else None
                if expected_old == rack:
                    return "RACK_PREPARATION_EVIDENCE_MISSING"
            else:
                mode = "REPLENISH_REQUIRED"
                expected_old = None
        else:
            if mode not in PREPARATIONS or plan["location_code"] != prep.point:
                return "RACK_PREPARATION_EVIDENCE_MISSING"
            expected_old = plan.get("current_rack", {}).get("rack_id")
            rack = plan.get("incoming_rack", {}).get("rack_id")
            face = plan.get("target_face") or plan.get("incoming_rack", {}).get("target_face")
        authorized = (
            point is not None
            and point.latest is not None
            and point.latest.id == prep.id
            and point.binding == intent.rack_basis["binding"]
            and point.config.location_code == prep.point
        )
        if mode != "REPLENISH_REQUIRED" and not self._matches(prep, old, expected_old):
            return "RACK_PREPARATION_EVIDENCE_MISSING"
        if mode in {"DEPART_REQUIRED", "REPLACE_REQUIRED"}:
            if old.pending:
                old = await self.observe(
                    session, old, now_ms, prep=prep, allow_departure=authorized
                )
            if old.pending or old.closure_reason != "DEPARTURE_ACK":
                action = old.actions[-1] if old.actions else None
                if (
                    action
                    and action.kind == "DEPARTURE_DECIDE"
                    and action.phase == "WAIT"
                    and action.due_at_ms is not None
                    and action.due_at_ms > now_ms
                ):
                    prep.next_check_at_ms = action.due_at_ms
                return "RACK_DEPARTURE_PENDING"
            if mode == "DEPART_REQUIRED":
                prep.pending = False
                return "RACK_DEPARTURE_ACCEPTED"

        if mode == "ROTATE_REQUIRED":
            job, kind = old, "ROTATE"
        else:
            kind = "ENTER"
            job = (
                await self.dispatch.progress(session, job_id=prep.incoming_job_id)
                if prep.incoming_job_id
                else None
            )
            if prep.incoming_job_id and not self._matches(prep, job, rack):
                return "RACK_PREPARATION_EVIDENCE_MISSING"
            if job is None and authorized:
                if await self.dispatch.has_conflicting_responsibility(
                    session, point=prep.point, rack_id=rack
                ) or await self.dispatch.has_position_conflict(session, point=prep.point):
                    return "RACK_POSITION_CONFLICT"
                prep.incoming_job_id = await self.dispatch.register(
                    session,
                    JobOrigin(
                        XRAY_SOURCES[0],
                        intent.operation_id,
                        reel.work_line_id,
                        rack,
                        None,
                        reel.workline_code,
                        prep.point,
                        face,
                        TransportCaller(reel.workline_code, prep.point),
                        str(uuid7()),
                        {
                            "positions": {"RETURN_RACK": prep.point},
                            "templates": point.config.templates,
                        },
                    ),
                )
                job = await self.dispatch.progress(session, job_id=prep.incoming_job_id)
        if job is None:
            return "RACK_CONFIGURATION_CHANGED"
        if prep.action_id is None and authorized and not await self._handoffs(session, prep.point):
            if implicit_entry and not await self.dispatch.point_vacant_for_entry(
                session, point=prep.point, rack_id=rack, exclude_job_id=job.id
            ):
                return "RACK_READINESS_REQUIRED"
            if kind == "ROTATE":
                ready = await self._ready(session, prep.work_line_id, prep.point, now_ms)
                if not ready or ready.id != job.id or ready.actual_face == face:
                    return "RACK_READINESS_REQUIRED"
            prep.action_id = await self.dispatch.prepare(
                session, job_id=job.id, kind=kind, source_ref=intent.operation_id, face=face
            )
        if prep.action_id is None:
            return "RACK_PREPARATION_PENDING"
        await self.observe(session, job, now_ms, prep=prep)
        ready = await self.dispatch.get_point_preparations(
            session, work_line_id=prep.work_line_id, point=prep.point, source_kinds=XRAY_SOURCES
        )
        if (
            authorized
            and len(ready) == 1
            and (
                ready[0].id == job.id
                and ready[0].position_action_id == prep.action_id
                and ready[0].actual_face == face
            )
        ):
            prep.pending = False
            return "RACK_READY"
        return "RACK_PREPARATION_PENDING"

    @staticmethod
    def _matches(prep, job, rack_id):
        return job is not None and (
            job.origin.work_line_id,
            job.origin.point,
            job.origin.rack_id,
        ) == (prep.work_line_id, prep.point, rack_id)
