"""Original direction work-plan intents; caller owns the AUTO transaction."""

from dataclasses import asdict

from pydantic import ValidationError
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.wms_request import WmsRequests
from app.models import WorkLine

from ._execution_facts import read_decision
from .inbound_contracts import SourceAbsent, Target
from .locking import lock_line
from .models import (
    AutoBinWork,
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoInboundSourceMember,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoTask,
    AutoTaskInput,
)
from .roller_ports import RollerPorts, matching_arrival
from .work_plan_contracts import DECISIONS, RackReady


async def outbound_locator_closed(
    session, *, task_id, bin_work_id, source_member_id, locator, before_plate_id=None
):
    model = AutoOutboundPlate
    query = select(model.source_locator).where(
        model.task_id == task_id,
        model.bin_work_id == bin_work_id,
        model.source_member_id == source_member_id,
        model.source_closed_basis.is_not(None),
    )
    if before_plate_id is not None:
        query = query.where(model.id < before_plate_id)
    return any(value == locator for value in await session.scalars(query))


class WorkPlans:
    def __init__(self, requests: WmsRequests, dispatch=None):
        self.requests = requests
        self.dispatch = dispatch
        self.inbound = None
        self.outbound = None
        self.roller = None

    async def start_bin(self, session, *, work_line_id, arrival_id, now_ms, trigger_plate_id=None):
        current_bindings = await self._lock_line(session, work_line_id)
        subject = await session.scalar(
            select(AutoBinWork)
            .where(AutoBinWork.work_line_id == work_line_id, AutoBinWork.arrival_id == arrival_id)
            .with_for_update()
        )
        if (
            subject is None
            or subject.association != "TASK"
            or subject.bin_code is None
            or subject.production_hold_reason is not None
            or subject.terminal_basis is not None
        ):
            return None
        task = await session.get(AutoTask, subject.task_id)
        line = await session.get(WorkLine, work_line_id)
        if (
            task is None
            or line is None
            or task.work_line_id != work_line_id
            or subject.business_type != task.business_type
            or not line.is_enabled
            or line.business_code != "smt_auto"
            or line.line_code != task.workline_code
        ):
            return None
        model = (
            AutoInboundDecisionRound
            if task.business_type == "inbound"
            else AutoOutboundDecisionRound
        )
        stage = f"bin:{subject.id}:first"
        original = await session.scalar(
            select(model)
            .where(model.task_id == task.task_id, model.stage_key == stage)
            .order_by(model.round_no.desc())
            .limit(1)
        )
        if trigger_plate_id is not None and task.business_type == "outbound":
            if not await self._outbound_trigger(
                session, task.task_id, trigger_plate_id, bin_work_id=subject.id
            ):
                return None
            existing = await session.scalar(
                select(model).where(
                    model.bin_work_id == subject.id, model.trigger_plate_id == trigger_plate_id
                )
            )
            if existing is not None:
                return existing
        elif trigger_plate_id is not None:
            if not await self.inbound_trigger(session, subject, trigger_plate_id):
                return None
            previous_trigger = await session.scalar(
                select(model).where(
                    model.bin_work_id == subject.id, model.trigger_plate_id == trigger_plate_id
                )
            )
            if previous_trigger is not None:
                return previous_trigger
        if original is not None and (
            original.unresolved
            or (trigger_plate_id is None and original.next_eligible_at_ms is None)
            or (original.next_eligible_at_ms is not None and now_ms < original.next_eligible_at_ms)
        ):
            return original
        if original is None:
            current = await RollerPorts().read_current_task(session, work_line_id=work_line_id)
            if task.status == "CANCELLED":
                if (
                    self.roller is None
                    or not await RollerPorts.prepared(session, task)
                    or not await matching_arrival(self.roller, session, subject)
                ):
                    return None
            elif current.task_id != task.task_id or not current.is_valid:
                return None
            if await self._confirmation(session, task.task_id):
                return None
        bindings = None
        if task.business_type == "inbound":
            sources, bindings = await self._inbound_sources(session, task, current_bindings)
            if sources is None:
                return None
            from .request.putaway_work_plan_v1 import WorkPlan

            data = dict(
                task_id=task.task_id,
                target_bin_code=subject.bin_code,
                scanned_at=subject.scanned_at,
                source_racks=sources,
            )
        else:
            from .request.outbound_bin_work_plan_v1 import WorkPlan

            data = dict(
                task_id=task.task_id, bin_code=subject.bin_code, scanned_at=subject.scanned_at
            )
        operation_id = str(uuid7())
        reference = await self.requests.send(session, WorkPlan(**data), operation_id=operation_id)
        round_ = model(
            task_id=task.task_id,
            purpose="WORK_PLAN",
            bin_work_id=subject.id,
            trigger_plate_id=trigger_plate_id,
            stage_key=stage,
            round_no=original.round_no + 1 if original else 1,
            previous_round_id=original.id if original else None,
            operation="putaway.work_plan@v1"
            if task.business_type == "inbound"
            else "outbound.bin.work_plan@v1",
            operation_id=operation_id,
            message_id=reference.message_id,
            request_data=data,
            source_rack_bindings=bindings,
            unresolved=True,
        )
        session.add(round_)
        await self._invalidate(session, work_line_id)
        await session.flush()
        return round_

    async def consume_bin(self, session, *, work_line_id, arrival_id, now_ms):
        await self._lock_line(session, work_line_id)
        subject = await session.scalar(
            select(AutoBinWork)
            .where(AutoBinWork.work_line_id == work_line_id, AutoBinWork.arrival_id == arrival_id)
            .with_for_update()
        )
        if subject is None or subject.association != "TASK":
            return None
        inbound = subject.business_type == "inbound"
        model = AutoInboundDecisionRound if inbound else AutoOutboundDecisionRound
        round_ = await session.scalar(
            select(model)
            .where(
                model.task_id == subject.task_id,
                model.bin_work_id == subject.id,
                model.purpose == "WORK_PLAN",
                model.stage_key == f"bin:{subject.id}:first",
            )
            .order_by(model.round_no.desc())
            .limit(1)
        )
        if round_ is None or not round_.unresolved:
            return round_
        expected = {
            "task_id": subject.task_id,
            "target_bin_code" if inbound else "bin_code": subject.bin_code,
            "scanned_at": subject.scanned_at,
        }
        if inbound:
            expected["source_racks"] = round_.request_data.get("source_racks")
        operation = "putaway.work_plan@v1" if inbound else "outbound.bin.work_plan@v1"
        if round_.request_data != expected or round_.operation != operation:
            return round_
        result = await self.requests.get_result(
            session,
            message_id=round_.message_id,
            operation=operation,
            operation_id=round_.operation_id,
        )
        if (
            (result.message_id, result.operation, result.operation_id)
            != (round_.message_id, operation, round_.operation_id)
            or result.availability != "FOUND"
            or result.response_state != "VALID"
            or (result.http_status, result.response_code) != (200, "DECIDED")
        ):
            return round_
        data = result.response_data
        try:
            DECISIONS["inbound" if inbound else "bin"].validate_python(data)
        except (ValidationError, TypeError):
            return round_
        if inbound and data["result"] == "READY":
            source = data["source"]
            if {
                "rack_id": source["rack_id"],
                "location_code": source["location_code"],
            } not in expected["source_racks"]:
                return round_
        if (
            not inbound
            and data["result"] == "READY"
            and await outbound_locator_closed(
                session,
                task_id=subject.task_id,
                bin_work_id=subject.id,
                source_member_id=None,
                locator={
                    "type": "BIN_CELL",
                    "bin_code": subject.bin_code,
                    "cell_id": data["cell_id"],
                },
            )
        ):
            return round_
        if inbound and data["result"] == "READY":
            proof = (round_.source_rack_bindings or {}).get(data["source"]["rack_id"])
            if (
                not isinstance(proof, dict)
                or not proof.get("rack_job_id")
                or not proof.get("preparation_action_id")
            ):
                return round_
            if await self.inbound_source_exhausted(
                session,
                task_id=subject.task_id,
                job_id=proof["rack_job_id"],
                preparation_action_id=proof["preparation_action_id"],
            ):
                return round_
        round_.response_snapshot = {
            "http_status": 200,
            "code": "DECIDED",
            "timestamp": result.response_timestamp,
            "data": data,
        }
        round_.response_recorded_at_ms = now_ms
        round_.consumed_at_ms = now_ms
        round_.unresolved = False
        if data["result"] == "WAIT":
            round_.wait_recorded_at_ms = now_ms
            round_.retry_after_ms = data["retry_after_ms"]
            round_.next_eligible_at_ms = now_ms + data["retry_after_ms"]
        elif data["result"] == "NO_WORK":
            subject.terminal_basis = {
                "kind": "NO_WORK",
                "round_id": round_.id,
                "operation_id": round_.operation_id,
            }
            subject.terminal_at_ms = now_ms
        else:
            source = data["source"] if inbound else data
            locator = (
                {"type": "BIN_CELL", "bin_code": subject.bin_code, "cell_id": source["cell_id"]}
                if not inbound
                else {
                    key: source[key]
                    for key in ("rack_id", "location_code", "bin_code", "bin_location", "cell_id")
                }
            )
            proof = (
                (round_.source_rack_bindings or {}).get(source.get("rack_id")) if inbound else None
            )
            plate = AutoInboundPlate if inbound else AutoOutboundPlate
            session.add(
                plate(
                    task_id=subject.task_id,
                    bin_work_id=subject.id,
                    source_rack_job_id=proof["rack_job_id"] if proof else None,
                    source_preparation_action_id=proof["preparation_action_id"] if proof else None,
                    grant_kind="READY",
                    grant_round_id=round_.id,
                    grant_snapshot=data,
                    source_locator=locator,
                    source_params={
                        key: value
                        for key, value in source.items()
                        if key not in {"result", *locator}
                    },
                    source_arrival_snapshot={
                        "arrival_id": subject.arrival_id,
                        "work_line_id": work_line_id,
                        "scanned_at": subject.scanned_at,
                        **({"source": proof} if proof else {}),
                    },
                    pick_call_key=f"auto:{subject.task_id}:work-plan:{round_.id}",
                    pending=True,
                )
            )
        await self._invalidate(session, work_line_id)
        await session.flush()
        return round_

    async def _lock_line(self, session, work_line_id):
        return await lock_line(session, self.dispatch, work_line_id)

    async def _source(self, session, task, member, *, inbound, current_bindings):
        if self.dispatch is None or not member.rack_job_id or not member.preparation_action_id:
            return None
        if inbound and await self.inbound_source_exhausted(
            session,
            task_id=task.task_id,
            job_id=member.rack_job_id,
            preparation_action_id=member.preparation_action_id,
        ):
            return None
        view = await self.dispatch.progress(session, job_id=member.rack_job_id)
        if view is None:
            return None
        action = next((a for a in view.actions if a.id == member.preparation_action_id), None)
        point = {"kind": "RACK_POSITION", "location_code": view.origin.point}
        face = (
            action.actual_face if inbound and action else member.rack_face if not inbound else None
        )
        kind = "AUTO_INBOUND_SOURCE" if inbound else "AUTO_OUTBOUND_RETURN"
        authorization = f"{'INBOUND_MEMBER' if inbound else 'OUTBOUND_MEMBER'}:{member.id}"
        if inbound and action is not None and action.source_ref != authorization:
            originals = list(
                await session.scalars(
                    select(AutoInboundSourceMember).where(
                        AutoInboundSourceMember.task_id == task.task_id,
                        AutoInboundSourceMember.rack_job_id == view.id,
                        AutoInboundSourceMember.preparation_action_id == action.id,
                        AutoInboundSourceMember.rack_id == member.rack_id,
                    )
                )
            )
            original = next(
                (item for item in originals if action.source_ref == f"INBOUND_MEMBER:{item.id}"),
                None,
            )
            plan = await session.get(AutoTaskInput, original.plan_input_id) if original else None
            if (
                plan is None
                or plan.task_id != task.task_id
                or plan.disposition != "APPLIED"
                or plan.applied_plan_revision != original.plan_revision
                or original.rack_array_index
                >= len(plan.effective_request.get("added_source_racks", []))
                or plan.effective_request["added_source_racks"][original.rack_array_index]
                != {"rack_id": member.rack_id}
            ):
                return None
            authorization = action.source_ref
        if (
            view.origin.source_kind != kind
            or view.origin.task_id != task.task_id
            or view.origin.work_line_id != task.work_line_id
            or view.origin.workline_code != task.workline_code
            or view.origin.rack_id != member.rack_id
            or not view.origin.point
            or view.state != "ACTIVE"
            or view.release_requested
            or view.review_reason is not None
            or action is None
            or action.kind not in ("ENTER", "ROTATE")
            or action.phase != "APPLIED"
            or not action.outcome
            or action.outcome.get("status") != "SUCCEEDED"
            or action.job_id != view.id
            or action.source_ref != authorization
            or action.target != point
            or (not inbound and action.target_face != face)
            or action.actual_position != point
            or action.actual_face != face
            or view.actual_position != point
            or view.actual_face != face
            or view.position_action_id != action.id
            or view.current_action_id != action.id
        ):
            return None
        try:
            binding = DeviceBinding(**view.origin.resources)
        except (TypeError, ValueError):
            return None
        line = await session.get(WorkLine, task.work_line_id)
        if (
            binding.business_code != "smt_auto"
            or binding.work_line_id != task.work_line_id
            or current_bindings.get(binding.device_code) != binding
            or line is None
            or view.origin.point not in line.position_bindings.values()
        ):
            return None
        return {
            "rack_id": member.rack_id,
            "location_code": view.origin.point,
            "rack_job_id": view.id,
            "preparation_action_id": action.id,
            "rack_face": face,
            "resources": asdict(binding),
        }

    async def inbound_source_exhausted(self, session, *, task_id, job_id, preparation_action_id):
        """Read original WMS exhaustion for this exact rack admission, never rack code alone."""
        progress = await self.dispatch.progress(session, job_id=job_id) if self.dispatch else None
        action = (
            next((a for a in progress.actions if a.id == preparation_action_id), None)
            if progress
            else None
        )
        if (
            progress is not None
            and action is not None
            and progress.origin.task_id == task_id
            and progress.origin.source_kind == "AUTO_INBOUND_SOURCE"
            and action.kind == "ENTER"
            and action.phase == "APPLIED"
            and action.outcome
            and action.outcome.get("status") == "SUCCEEDED"
            and action.actual_position
            == action.target
            == {"kind": "RACK_POSITION", "location_code": progress.origin.point}
        ):
            for notice in await session.scalars(
                select(AutoTaskInput).where(
                    AutoTaskInput.task_id == task_id,
                    AutoTaskInput.operation == "putaway.source.rack_exhausted@v1",
                    AutoTaskInput.source_rack_job_id == job_id,
                    AutoTaskInput.source_preparation_action_id == preparation_action_id,
                )
            ):
                if notice.disposition == "APPLIED" and notice.effective_request == dict(
                    task_id=task_id,
                    rack_id=progress.origin.rack_id,
                    transport_task_id=action.transport_task_id,
                ):
                    return True
        plates = await session.scalars(
            select(AutoInboundPlate).where(
                AutoInboundPlate.task_id == task_id,
                AutoInboundPlate.source_rack_job_id == job_id,
                AutoInboundPlate.source_preparation_action_id == preparation_action_id,
            )
        )
        for plate in plates:
            round_ = (
                await session.get(AutoInboundDecisionRound, plate.final_decision_round_id)
                if plate.final_decision_round_id is not None
                else None
            )
            source = plate.source_locator
            proof = plate.source_arrival_snapshot.get("source", {})
            if (
                round_ is None
                or round_.task_id != task_id
                or round_.plate_id != plate.id
                or (round_.purpose, round_.operation)
                not in (
                    ("MATERIAL_DECIDE", "putaway.material.decide@v1"),
                    ("EMPTY_DECIDE", "putaway.source.empty_decide@v1"),
                )
                or round_.unresolved
                or round_.consumed_at_ms is None
                or proof.get("rack_job_id") != job_id
                or proof.get("preparation_action_id") != preparation_action_id
                or proof.get("rack_id") != source.get("rack_id")
                or proof.get("location_code") != source.get("location_code")
                or round_.request_data.get("source")
                != {key: source.get(key) for key in ("rack_id", "bin_code", "cell_id")}
                or round_.request_data.get("task_id") != task_id
                or (
                    round_.purpose == "MATERIAL_DECIDE"
                    and (
                        round_.request_data.get("barcode") != plate.barcode
                        or round_.request_data.get("scanned_at") != plate.scanned_at_ms
                    )
                )
                or (
                    round_.purpose == "EMPTY_DECIDE"
                    and (
                        round_.request_data != plate.empty_observation
                        or round_.request_data.get("observed_at")
                        != (plate.pick_result_snapshot or {}).get("finish_time")
                    )
                )
            ):
                continue
            progress = (
                await self.dispatch.progress(session, job_id=job_id) if self.dispatch else None
            )
            action = (
                next((a for a in progress.actions if a.id == preparation_action_id), None)
                if progress
                else None
            )
            point = {"kind": "RACK_POSITION", "location_code": source.get("location_code")}
            if (
                progress is None
                or progress.origin.source_kind != "AUTO_INBOUND_SOURCE"
                or progress.origin.task_id != task_id
                or progress.origin.rack_id != source.get("rack_id")
                or progress.origin.point != source.get("location_code")
                or progress.origin.resources != proof.get("resources")
                or action is None
                or action.job_id != job_id
                or action.kind not in ("ENTER", "ROTATE")
                or action.phase != "APPLIED"
                or action.target != point
                or action.actual_position != point
                or not action.outcome
                or action.outcome.get("status") != "SUCCEEDED"
            ):
                continue
            result = await self.requests.get_result(
                session,
                message_id=round_.message_id,
                operation=round_.operation,
                operation_id=round_.operation_id,
            )
            data = result.response_data
            if (
                (result.message_id, result.operation, result.operation_id)
                != (round_.message_id, round_.operation, round_.operation_id)
                or result.availability != "FOUND"
                or result.response_state != "VALID"
                or (result.http_status, result.response_code) != (200, "DECIDED")
                or not isinstance(data, dict)
                or data.get("result")
                != ("SOURCE_ABSENT" if round_.purpose == "EMPTY_DECIDE" else "ACCEPT")
                or data.get("source_rack_exhausted") is not True
                or set(data)
                - (
                    {"result", "source_rack_exhausted"}
                    if round_.purpose == "EMPTY_DECIDE"
                    else {"result", "target", "source_rack_exhausted", "next_source_action"}
                )
                or (round_.response_snapshot or {}).get("data") != data
            ):
                continue
            try:
                target = (
                    None
                    if round_.purpose == "EMPTY_DECIDE"
                    else Target.model_validate(data.get("target"))
                )
                if target is None:
                    SourceAbsent.model_validate(data)
            except (ValidationError, TypeError):
                continue
            work = await session.get(AutoBinWork, plate.bin_work_id)
            if (
                work is not None
                and work.task_id == task_id
                and progress.origin.work_line_id == work.work_line_id
                and plate.source_arrival_snapshot.get("work_line_id") == work.work_line_id
                and (
                    target is None
                    or (
                        target.bin_code == work.bin_code
                        and round_.request_data.get("target_bin_code") == work.bin_code
                    )
                )
            ):
                return True
        return False

    async def _inbound_sources(self, session, task, current_bindings, *, original=None):
        if self.dispatch is None:
            return None, None
        query = select(AutoInboundSourceMember).where(
            AutoInboundSourceMember.task_id == task.task_id
        )
        if original is not None:
            query = query.where(
                AutoInboundSourceMember.rack_id == original.get("rack_id"),
                AutoInboundSourceMember.rack_job_id == original.get("rack_job_id"),
                AutoInboundSourceMember.preparation_action_id
                == original.get("preparation_action_id"),
            )
        members = tuple(
            await session.scalars(query.order_by(AutoInboundSourceMember.id).with_for_update())
        )
        sources, bindings = [], {}
        for member in members:
            withdrawn_at = member.withdrawn_at_ms is not None
            withdrawn_by = member.withdrawn_by_input_id is not None
            if withdrawn_at != withdrawn_by or (original is None and withdrawn_at):
                continue
            plan = await session.get(AutoTaskInput, member.plan_input_id)
            if (
                plan is None
                or plan.task_id != task.task_id
                or plan.disposition != "APPLIED"
                or plan.applied_plan_revision != member.plan_revision
            ):
                continue
            proof = await self._source(
                session, task, member, inbound=True, current_bindings=current_bindings
            )
            if proof is not None and proof["rack_id"] not in bindings:
                sources.append({key: proof[key] for key in ("rack_id", "location_code")})
                bindings[proof["rack_id"]] = proof
        if len(sources) > 2 or len({item["location_code"] for item in sources}) != len(sources):
            return None, None
        return sources, bindings

    async def inbound_trigger(self, session, work, plate_id, *, before_plate_id=None):
        trigger = await session.get(AutoInboundPlate, plate_id)
        if (
            self.inbound is None
            or trigger is None
            or trigger.bin_work_id != work.id
            or trigger.task_id != work.task_id
            or (before_plate_id is not None and trigger.id >= before_plate_id)
            or not await self.inbound.closed(session, trigger)
        ):
            return False
        final = await session.get(AutoInboundDecisionRound, trigger.final_decision_round_id)
        return final is not None and (final.response_snapshot or {}).get("data", {}).get(
            "result"
        ) in ("REJECT", "SOURCE_ABSENT")

    async def _outbound_trigger(
        self,
        session,
        task_id,
        plate_id,
        *,
        bin_work_id=None,
        source_member_id=None,
        before_plate_id=None,
    ):
        trigger = await session.get(AutoOutboundPlate, plate_id)
        return bool(
            self.outbound
            and trigger
            and trigger.task_id == task_id
            and (before_plate_id is None or trigger.id < before_plate_id)
            and trigger.bin_work_id == bin_work_id
            and trigger.source_member_id == source_member_id
            and await self.outbound.closed(session, trigger)
            and (
                (trigger.source_closed_basis or {}).get("kind") == "SOURCE_DONE"
                if trigger.empty_observation is not None
                else trigger.report_data["to_locator"]["type"] == "NG_ZONE"
            )
        )

    async def start_return_face(
        self,
        session,
        *,
        work_line_id,
        member_id,
        now_ms,
        trigger_plate_id=None,
        correct_operation_id=None,
    ):
        current_bindings = await self._lock_line(session, work_line_id)
        member = await session.get(AutoOutboundSourceMember, member_id, with_for_update=True)
        if member is None or member.terminal_basis is not None:
            return None
        if type(member.arrived_at_ms) is not int or not 0 <= member.arrived_at_ms <= 2**63 - 1:
            return None
        task = await session.get(AutoTask, member.task_id)
        if task is None or task.work_line_id != work_line_id or task.business_type != "outbound":
            return None
        first = (
            await session.get(AutoOutboundDecisionRound, member.first_work_plan_round_id)
            if member.first_work_plan_round_id
            else None
        )
        stage = first.stage_key if first else f"face:{member.preparation_action_id}:first"
        original = await session.scalar(
            select(AutoOutboundDecisionRound)
            .where(
                AutoOutboundDecisionRound.task_id == task.task_id,
                AutoOutboundDecisionRound.stage_key == stage,
            )
            .order_by(AutoOutboundDecisionRound.round_no.desc())
            .limit(1)
        )
        if trigger_plate_id is not None:
            if not await self._outbound_trigger(
                session, task.task_id, trigger_plate_id, source_member_id=member.id
            ):
                return None
            existing = await session.scalar(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.task_id == task.task_id,
                    AutoOutboundDecisionRound.source_preparation_action_id
                    == member.preparation_action_id,
                    AutoOutboundDecisionRound.trigger_plate_id == trigger_plate_id,
                )
            )
            if existing is not None:
                return existing
        correction = None
        if correct_operation_id is not None:
            if (
                first is None
                or first.operation_id != correct_operation_id
                or trigger_plate_id is not None
                or member.withdrawn_at_ms is not None
                or member.withdrawn_by_input_id is not None
                or task.status != "EXECUTING"
                or not task.pending
                or await self._confirmation(session, task.task_id)
            ):
                return None
            if original is not first:
                if original and original.previous_round_id == first.id:
                    result = await self._invalid_first_rack_ready(session, first)
                    if result and await self.requests.has_decision_correction(
                        session,
                        original=result,
                        replacement_operation_id=original.operation_id,
                        replacement_message_id=original.message_id,
                    ):
                        return original
                return None
            if not first.unresolved:
                return None
            correction = await self._invalid_first_rack_ready(session, first)
            if correction is None or (
                correction.response_data["rack_id"],
                correction.response_data["rack_face"],
            ) != (member.rack_id, member.rack_face):
                return None

        if (
            correction is None
            and original is not None
            and (
                original.unresolved
                or (trigger_plate_id is None and original.next_eligible_at_ms is None)
                or (
                    original.next_eligible_at_ms is not None
                    and now_ms < original.next_eligible_at_ms
                )
            )
        ):
            return original
        if original is None:
            current = await RollerPorts().read_current_task(session, work_line_id=work_line_id)
            plan = await session.get(AutoTaskInput, member.plan_input_id)
            if (
                current.task_id != task.task_id
                or not current.is_valid
                or await self._confirmation(session, task.task_id)
                or member.withdrawn_at_ms is not None
                or member.withdrawn_by_input_id is not None
                or plan is None
                or plan.task_id != task.task_id
                or plan.disposition != "APPLIED"
                or plan.applied_plan_revision != member.plan_revision
            ):
                return None
        proof = await self._source(
            session, task, member, inbound=False, current_bindings=current_bindings
        )
        if proof is None:
            return None
        from .request.outbound_return_rack_work_plan_v1 import WorkPlan

        data = (
            original.request_data
            if original
            else dict(
                task_id=task.task_id,
                rack_id=member.rack_id,
                rack_face=member.rack_face,
                arrived_at=member.arrived_at_ms,
            )
        )
        operation_id = str(uuid7())
        if correction is None:
            reference = await self.requests.send(
                session, WorkPlan(**data), operation_id=operation_id
            )
        else:
            if original.source_rack_bindings != proof or original.request_data != dict(
                task_id=task.task_id,
                rack_id=member.rack_id,
                rack_face=member.rack_face,
                arrived_at=member.arrived_at_ms,
            ):
                return None
            reference = await self.requests.record_decision_correction(
                session,
                WorkPlan(**data),
                original=correction,
                replacement_operation_id=operation_id,
            )
            if reference is None:
                return None
            original.unresolved = False
            await session.flush()
        round_ = AutoOutboundDecisionRound(
            task_id=task.task_id,
            purpose="WORK_PLAN",
            source_preparation_action_id=member.preparation_action_id,
            trigger_plate_id=trigger_plate_id,
            stage_key=stage,
            round_no=original.round_no + 1 if original else 1,
            previous_round_id=original.id if original else None,
            operation="outbound.return_rack.work_plan@v1",
            operation_id=operation_id,
            message_id=reference.message_id,
            request_data=data,
            source_rack_bindings=proof,
            unresolved=True,
        )
        session.add(round_)
        await session.flush()
        if member.first_work_plan_round_id is None:
            member.first_work_plan_round_id = round_.id
        await self.dispatch.note_facts_changed(session, job_ids=(member.rack_job_id,))
        await session.flush()
        return round_

    async def _invalid_first_rack_ready(self, session, round_):
        if (
            round_.operation != "outbound.return_rack.work_plan@v1"
            or round_.purpose != "WORK_PLAN"
            or round_.round_no != 1
            or round_.previous_round_id is not None
            or round_.trigger_plate_id is not None
            or round_.consumed_at_ms is not None
            or round_.response_snapshot is not None
            or await session.scalar(
                select(AutoOutboundPlate.id)
                .where(AutoOutboundPlate.grant_round_id == round_.id)
                .limit(1)
            )
            is not None
        ):
            return None
        result = await read_decision(self.requests, session, round_)
        data = result.response_data if result else None
        if (
            not isinstance(data, dict)
            or "reel_thickness" not in data
            or "reel_diameter" not in data
            or data["reel_thickness"] is not None
            or data["reel_diameter"] is not None
        ):
            return None
        try:
            RackReady.model_validate({**data, "reel_thickness": 0, "reel_diameter": 0})
        except ValidationError:
            return None
        return result

    async def corrected_rack_ready(self, session, previous, current, data=None):
        data = data if data is not None else (current.response_snapshot or {}).get("data", {})
        if (
            data.get("result") != "READY"
            or current.previous_round_id != previous.id
            or previous.unresolved
            or previous.source_rack_bindings != current.source_rack_bindings
            or previous.request_data != current.request_data
        ):
            return False
        result = await self._invalid_first_rack_ready(session, previous)
        return (
            result is not None
            and result.response_data == {**data, "reel_thickness": None, "reel_diameter": None}
            and await self.requests.has_decision_correction(
                session,
                original=result,
                replacement_operation_id=current.operation_id,
                replacement_message_id=current.message_id,
            )
        )

    async def consume_return_face(self, session, *, work_line_id, member_id, now_ms):
        await self._lock_line(session, work_line_id)
        member = await session.get(AutoOutboundSourceMember, member_id, with_for_update=True)
        if member is None or member.first_work_plan_round_id is None:
            return None
        task = await session.get(AutoTask, member.task_id)
        if task is None or task.work_line_id != work_line_id or task.business_type != "outbound":
            return None
        first = await session.get(AutoOutboundDecisionRound, member.first_work_plan_round_id)
        if first is None or first.task_id != member.task_id:
            return None
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound)
            .where(
                AutoOutboundDecisionRound.task_id == member.task_id,
                AutoOutboundDecisionRound.source_preparation_action_id
                == first.source_preparation_action_id,
                AutoOutboundDecisionRound.stage_key == first.stage_key,
                AutoOutboundDecisionRound.purpose == "WORK_PLAN",
            )
            .order_by(AutoOutboundDecisionRound.round_no.desc())
            .limit(1)
        )
        if round_ is None or not round_.unresolved:
            return round_
        request = round_.request_data
        if (
            set(request) != {"task_id", "rack_id", "rack_face", "arrived_at"}
            or request["task_id"] != member.task_id
            or request["rack_id"] != member.rack_id
            or request["rack_face"] != member.rack_face
            or type(request["arrived_at"]) is not int
            or not 0 <= request["arrived_at"] <= 2**63 - 1
            or round_.operation != "outbound.return_rack.work_plan@v1"
        ):
            return round_
        result = await self.requests.get_result(
            session,
            message_id=round_.message_id,
            operation=round_.operation,
            operation_id=round_.operation_id,
        )
        if (
            (result.message_id, result.operation, result.operation_id)
            != (round_.message_id, round_.operation, round_.operation_id)
            or result.availability != "FOUND"
            or result.response_state != "VALID"
            or (result.http_status, result.response_code) != (200, "DECIDED")
        ):
            return round_
        data = result.response_data
        try:
            DECISIONS["rack"].validate_python(data)
        except (ValidationError, TypeError):
            return round_
        if (
            round_.previous_round_id == first.id
            and await self._invalid_first_rack_ready(session, first)
            and not await self.corrected_rack_ready(session, first, round_, data)
        ):
            return round_
        if data["result"] == "READY" and (data["rack_id"], data["rack_face"]) != (
            member.rack_id,
            member.rack_face,
        ):
            return round_
        if data["result"] == "READY" and await outbound_locator_closed(
            session,
            task_id=member.task_id,
            bin_work_id=None,
            source_member_id=member.id,
            locator={
                "type": "RACK_SLOT",
                **{key: data[key] for key in ("rack_id", "rack_face", "slot_id")},
            },
        ):
            return round_
        round_.response_snapshot = {
            "http_status": 200,
            "code": "DECIDED",
            "timestamp": result.response_timestamp,
            "data": data,
        }
        round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
        round_.unresolved = False
        if data["result"] == "WAIT":
            round_.wait_recorded_at_ms = now_ms
            round_.retry_after_ms = data["retry_after_ms"]
            round_.next_eligible_at_ms = now_ms + data["retry_after_ms"]
        elif data["result"] == "NO_WORK":
            member.terminal_basis = {
                "kind": "NO_WORK",
                "round_id": round_.id,
                "operation_id": round_.operation_id,
            }
            member.terminal_at_ms = now_ms
        else:
            session.add(
                AutoOutboundPlate(
                    task_id=member.task_id,
                    source_member_id=member.id,
                    source_rack_job_id=round_.source_rack_bindings["rack_job_id"],
                    source_preparation_action_id=round_.source_preparation_action_id,
                    grant_kind="READY",
                    grant_round_id=round_.id,
                    grant_snapshot=data,
                    source_locator={
                        "type": "RACK_SLOT",
                        **{key: data[key] for key in ("rack_id", "rack_face", "slot_id")},
                    },
                    source_params={
                        key: data[key]
                        for key in ("rack_layer", "rack_column", "reel_thickness", "reel_diameter")
                    },
                    source_arrival_snapshot=round_.source_rack_bindings,
                    pick_call_key=f"auto:{member.task_id}:work-plan:{round_.id}",
                    pending=True,
                )
            )
        await self.dispatch.note_facts_changed(session, job_ids=(member.rack_job_id,))
        await session.flush()
        return round_

    async def _invalidate(self, session, work_line_id):
        if self.dispatch is not None:
            jobs = await self.dispatch.admission_jobs(session, work_line_id=work_line_id)
            await self.dispatch.note_facts_changed(session, job_ids=tuple(job.id for job in jobs))

    async def _confirmation(self, session, task_id):
        return await RollerPorts().confirmation_pending(session, task_id=task_id)
