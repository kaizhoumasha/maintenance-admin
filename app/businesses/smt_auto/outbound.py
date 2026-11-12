"""Outbound original plate production; caller owns the ordered SSI transaction."""

from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.ecs_callback import EventApplicationRef
from app.integrations.ecs_command import CommandOwner
from app.integrations.ecs_command.contracts import ResultEnvelope
from app.models import WorkLine

from ._execution_facts import arrival_is_open, observe_arm_result, read_decision
from .ecs_command import ArmResult, PickAndPut, ecs_reel_diameter
from .inbound import PhysicalSettings
from .inbound_contracts import Scan
from .locking import lock_line
from .models import (
    AutoBinWork,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
)
from .outbound_contracts import DECISIONS, EMPTY_DECISION
from .request.outbound_bin_material_decide_v1 import MaterialDecide as BinDecide
from .request.outbound_material_movement_report_v1 import MovementReport
from .request.outbound_rackslot_material_decide_v1 import MaterialDecide as RackDecide
from .work_plan_contracts import DECISIONS as PLANS
from .work_plan_contracts import Wait
from .work_plans import WorkPlans, outbound_locator_closed

OWNER = CommandOwner("smt_auto")
REPORT = "outbound.material.movement_report@v1"
EMPTY = "outbound.source.empty_decide@v1"
SCAN = {"location_id": "STATION_SCAN", "location_type": "SCAN_PLATFORM"}
NG = {"location_id": "STATION_NG", "location_type": "SCAN_NG"}


class Outbound:
    def __init__(self, requests, ecs, dispatch, roller, *, settings=()):
        values = (settings,) if isinstance(settings, PhysicalSettings) else tuple(settings)
        if any(not isinstance(item, PhysicalSettings) for item in values) or len(
            {item.work_line_id for item in values}
        ) != len(values):
            raise ValueError("unique explicit physical settings required")
        self.settings = {item.work_line_id: item for item in values}
        self.requests, self.ecs, self.dispatch, self.roller = requests, ecs, dispatch, roller
        self.work_plans = WorkPlans(requests, dispatch)
        self.work_plans.outbound = self

    async def _result(self, session, plate, phase, *, empty=False):
        code = getattr(plate, f"{phase}_command_code")
        key = getattr(plate, f"{phase}_call_key")
        snapshot = plate.source_arrival_snapshot or {}
        command = snapshot.get(f"{phase}_command", {})
        if not code or not key or not command:
            return None
        view = await self.ecs.get_by_call(session, owner=OWNER, call_key=key)
        if (
            view is None
            or view.command_code != code
            or view.status != ("FAILED" if empty else "SUCCEEDED")
            or view.device_code != command.get("device_code")
            or view.task_type != "PICK_AND_PUT"
            or view.hold_reason
            or view.needs_reconciliation
            or view.result is None
        ):
            return None
        try:
            result = ResultEnvelope.model_validate(view.result)
            data = ArmResult.model_validate(result.data)
            if phase == "put" and set(result.data) != {"actual_qty", "pick_and_put_result"}:
                return None
        except (ValidationError, TypeError):
            return None
        if (
            result.command_code != code
            or result.device_code != view.device_code
            or result.result != ("FAILED" if empty else "SUCCESS")
            or data.actual_qty != (0 if empty else 1)
            or data.pick_and_put_result != ("PICK_FAILED" if empty else "PUT_FINISHED")
        ):
            return None
        return result

    async def accept_platform_scan(self, ctx, payload):
        # The receiver's binding resolver holds the WorkLine and scanner locks.
        plates = (
            await ctx.session.scalars(
                select(AutoOutboundPlate)
                .join(AutoTask, AutoTask.task_id == AutoOutboundPlate.task_id)
                .join(AutoBinWork, AutoBinWork.id == AutoOutboundPlate.bin_work_id)
                .where(
                    AutoOutboundPlate.pick_command_code == payload.previous_command_code,
                    AutoOutboundPlate.pending,
                    AutoTask.work_line_id == ctx.work_line_id,
                    AutoTask.business_type == "outbound",
                    AutoTask.status.in_(("EXECUTING", "CANCELLED")),
                    AutoBinWork.task_id == AutoTask.task_id,
                    AutoBinWork.work_line_id == ctx.work_line_id,
                    AutoBinWork.terminal_at_ms.is_(None),
                )
                .limit(2)
                .with_for_update(of=AutoOutboundPlate)
            )
        ).all()
        if len(plates) != 1:
            ctx.note("PLATFORM_SCAN_PICK_NOT_FOUND")
            return
        plate = plates[0]
        snapshot = plate.source_arrival_snapshot
        command = snapshot.get("pick_command", {})
        params = command.get("params", {})
        if (
            params.get("source", {}).get("location_type") != "BIN"
            or params.get("target") != SCAN
            or command.get("platform_scanner_id", ctx.binding.device_id) != ctx.binding.device_id
        ):
            ctx.note("PLATFORM_SCAN_PICK_MISMATCH")
            return

        event_id = command.get("platform_scan_event_id")
        scan = (payload.barcode, ctx.timestamp)
        if (event_id is not None or plate.pick_consumed_at_ms is not None) and (
            plate.barcode,
            plate.scanned_at_ms,
        ) != scan:
            ctx.note("PLATFORM_SCAN_CONFLICT")
            return
        if event_id is None:
            event_id = ctx.receipt_id
            plate.barcode, plate.scanned_at_ms = scan
            plate.source_arrival_snapshot = {
                **snapshot,
                "pick_command": {**command, "platform_scan_event_id": event_id},
            }
        ctx.bind_application(EventApplicationRef("AUTO_OUTBOUND_PLATE", str(plate.id), event_id))

    async def _pick_scan(self, session, plate):
        pick = await self._result(session, plate, "pick")
        if pick is None:
            return None
        command = plate.source_arrival_snapshot["pick_command"]
        if "platform_scan_event_id" in command:
            data = {**pick.data, "barcode": plate.barcode, "scanned_at": plate.scanned_at_ms}
        elif "platform_scanner_id" in command:
            return None
        else:
            # Commands registered before scanner binding can still carry their original scan.
            data = pick.data
        try:
            return Scan.model_validate(data)
        except ValidationError:
            return None

    async def _grant(self, session, plate, *, new_pick=False):
        if plate.grant_kind == "RETRY":
            grant = await session.get(AutoOutboundDecisionRound, plate.grant_round_id)
            parent = (
                await session.get(AutoOutboundPlate, grant.plate_id)
                if grant is not None and grant.plate_id is not None
                else None
            )
            return bool(
                parent
                and parent.id < plate.id
                and parent.final_decision_round_id == grant.id
                and plate.grant_snapshot == {"result": "RETRY"}
                and plate.task_id == parent.task_id
                and plate.bin_work_id == parent.bin_work_id
                and plate.source_member_id == parent.source_member_id
                and plate.source_rack_job_id == parent.source_rack_job_id
                and plate.source_preparation_action_id == parent.source_preparation_action_id
                and plate.source_locator == parent.source_locator
                and plate.source_params == parent.source_params
                and self._arrival_context(plate) == self._arrival_context(parent)
                and await self._grant(session, parent, new_pick=new_pick)
                and await self._empty_closed(session, parent) == "RETRY"
            )
        if plate.grant_kind == "READY":
            grant = await session.get(AutoOutboundDecisionRound, plate.grant_round_id)
            expected = plate.grant_snapshot
        elif plate.grant_kind == "NEXT":
            parent = await session.get(AutoOutboundPlate, plate.grant_parent_plate_id)
            if (
                parent is None
                or parent.task_id != plate.task_id
                or parent.bin_work_id != plate.bin_work_id
                or parent.source_member_id != plate.source_member_id
            ):
                return False
            grant = await session.get(AutoOutboundDecisionRound, parent.final_decision_round_id)
            if grant is None or grant.plate_id != parent.id:
                return False
            expected = (grant.response_snapshot or {}).get("data", {})
            if (
                expected.get("result") != "ACCEPT"
                or expected.get("next_source_action") != plate.grant_snapshot
                or "result" in plate.grant_snapshot
            ):
                return False
        else:
            return False
        if (
            grant is None
            or grant.task_id != plate.task_id
            or grant.unresolved
            or grant.consumed_at_ms is None
        ):
            return False
        if plate.grant_kind == "READY":
            if grant.purpose != "WORK_PLAN":
                return False
            if plate.bin_work_id is not None:
                work = await session.get(AutoBinWork, plate.bin_work_id)
                if (
                    work is None
                    or work.task_id != plate.task_id
                    or grant.bin_work_id != work.id
                    or grant.operation != "outbound.bin.work_plan@v1"
                    or grant.request_data
                    != dict(
                        task_id=plate.task_id, bin_code=work.bin_code, scanned_at=work.scanned_at
                    )
                    or plate.source_locator.get("bin_code") != work.bin_code
                ):
                    return False
            else:
                member = await session.get(AutoOutboundSourceMember, plate.source_member_id)
                if (
                    member is None
                    or member.task_id != plate.task_id
                    or grant.operation != "outbound.return_rack.work_plan@v1"
                    or grant.source_preparation_action_id != member.preparation_action_id
                    or plate.source_rack_job_id != member.rack_job_id
                    or plate.source_preparation_action_id != member.preparation_action_id
                    or grant.request_data
                    != dict(
                        task_id=plate.task_id,
                        rack_id=member.rack_id,
                        rack_face=member.rack_face,
                        arrived_at=member.arrived_at_ms,
                    )
                ):
                    return False
            if new_pick and not await self._work_plan_pick_ready(session, plate, grant):
                return False
        elif not await self._grant(session, parent, new_pick=new_pick):
            return False
        result = await read_decision(self.requests, session, grant)
        if (
            result is None
            or result.response_data != expected
            or expected != (grant.response_snapshot or {}).get("data")
        ):
            return False
        ready = (
            plate.grant_snapshot
            if plate.grant_kind == "READY"
            else {"result": "READY", **plate.grant_snapshot}
        )
        try:
            plan = PLANS["bin" if plate.bin_work_id else "rack"].validate_python(
                ready, extra="ignore" if plate.grant_kind == "NEXT" else None
            )
        except (ValueError, TypeError):
            return False
        fields = {k: ready[k] for k in type(plan).model_fields if k != "result"}
        locator = {k: v for k, v in plate.source_locator.items() if k not in ("type", "bin_code")}
        return fields == {**locator, **plate.source_params}

    async def _work_plan_pick_ready(self, session, plate, round_):
        # WAIT keeps the same exception trigger, even though each evaluation has a new ID.
        while round_.trigger_plate_id is None and round_.previous_round_id is not None:
            previous = await session.get(AutoOutboundDecisionRound, round_.previous_round_id)
            if (
                previous is None
                or previous.round_no + 1 != round_.round_no
                or previous.task_id != round_.task_id
                or previous.stage_key != round_.stage_key
                or previous.purpose != "WORK_PLAN"
                or previous.operation != round_.operation
                or previous.bin_work_id != round_.bin_work_id
                or previous.source_preparation_action_id != round_.source_preparation_action_id
                or previous.request_data != round_.request_data
                or not await self._recorded_wait(session, previous)
            ):
                return False
            round_ = previous
        if round_.trigger_plate_id is None:
            return round_.round_no == 1
        return await self.work_plans._outbound_trigger(
            session,
            plate.task_id,
            round_.trigger_plate_id,
            bin_work_id=plate.bin_work_id,
            source_member_id=plate.source_member_id,
            before_plate_id=plate.id,
        )

    async def _source_ready(self, session, task, plate, bindings):
        if await outbound_locator_closed(
            session,
            task_id=task.task_id,
            bin_work_id=plate.bin_work_id,
            source_member_id=plate.source_member_id,
            locator=plate.source_locator,
            before_plate_id=plate.id,
        ):
            return False
        if plate.bin_work_id:
            work = await session.get(AutoBinWork, plate.bin_work_id)
            return bool(
                work
                and work.task_id == task.task_id
                and work.business_type == "outbound"
                and not work.production_hold_reason
                and not work.terminal_basis
                and await arrival_is_open(self.roller, session, work)
            )
        member = await session.get(AutoOutboundSourceMember, plate.source_member_id)
        if (
            member is None
            or member.task_id != task.task_id
            or member.terminal_basis
            or member.withdrawn_at_ms is not None
            or member.withdrawn_by_input_id is not None
        ):
            return False
        proof = await self.work_plans._source(
            session, task, member, inbound=False, current_bindings=bindings
        )
        return (
            proof is not None
            and proof == self._arrival_context(plate)
            and proof["rack_job_id"] == plate.source_rack_job_id
            and proof["preparation_action_id"] == plate.source_preparation_action_id
        )

    async def _target_ready(self, session, task, plate, bindings):
        target = plate.target_snapshot or {}
        jobs = await self.dispatch.jobs(
            session, work_line_id=task.work_line_id, source_kinds=("AUTO_OUTBOUND_TARGET",)
        )
        role = next((b for b in bindings.values() if b and b.device_role == "RACK_ARM"), None)
        if role is None:
            return False
        eligible = []
        for job in jobs:
            if plate.target_rack_job_id is not None and plate.target_rack_job_id != job.id:
                continue
            action = next((a for a in job.actions if a.id == job.position_action_id), None)
            point = {"kind": "RACK_POSITION", "location_code": job.origin.point}
            resources = job.origin.resources
            if (
                job.origin.task_id == task.task_id
                and job.origin.rack_id == target.get("rack_id")
                and job.state == "ACTIVE"
                and not job.release_requested
                and job.review_reason is None
                and action
                and action.kind in ("ENTER", "ROTATE")
                and action.phase == "APPLIED"
                and action.outcome
                and action.outcome.get("status") == "SUCCEEDED"
                and action.actual_position == point
                and action.target == point
                and action.actual_face == target.get("rack_face")
                and action.target_face == target.get("rack_face")
                and job.actual_position == point
                and job.actual_face == target.get("rack_face")
                and job.current_action_id == action.id
                and resources.get("device_id") == role.device_id
                and resources.get("device_code") == role.device_code
                and resources.get("device_version") == role.device_version
                and resources.get("device_role") == role.device_role
                and resources.get("work_line_id") == role.work_line_id
                and resources.get("business_code") == role.business_code
                and resources.get("position_bindings", {}).get("TRANSFER_RACK") == job.origin.point
                and role.position_bindings.get("TRANSFER_RACK") == job.origin.point
            ):
                eligible.append((job, action))
        if len(eligible) != 1:
            return False
        job, action = eligible[0]
        final = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
        preparation = (
            (final.response_snapshot or {}).get("data", {}).get("target_preparation")
            if final
            else None
        )
        if preparation is not None and (
            action.source_ref != f"PLATE_TARGET:{plate.id}"
            or action.kind != ("ROTATE" if preparation == "ROTATE" else "ENTER")
        ):
            return False
        if plate.target_preparation_action_id not in (None, action.id):
            return False
        plate.target_rack_job_id, plate.target_preparation_action_id = job.id, action.id
        return True

    async def _pick_role(self, session, task, plate, bindings, scanner):
        is_bin = plate.bin_work_id is not None
        if task.status != "EXECUTING" and not (task.status == "CANCELLED" and is_bin):
            return None
        if is_bin and (scanner is None or scanner.device_role != "PLATFORM_SCANNER"):
            return None
        role_name = "BIN_ARM" if is_bin else "RACK_ARM"
        role = next((b for b in bindings.values() if b and b.device_role == role_name), None)
        if (
            role
            and await self._grant(session, plate, new_pick=True)
            and await self._source_ready(session, task, plate, bindings)
        ):
            return role
        return None

    @staticmethod
    def _pick_priority(plate):
        if plate.bin_work_id is None:
            return (1, plate.id)
        return (0 if plate.grant_kind in ("NEXT", "RETRY") else 2, plate.id)

    async def _platform_clear(self, session, work_line_id):
        outstanding = await session.scalars(
            select(AutoOutboundPlate)
            .join(AutoTask, AutoTask.task_id == AutoOutboundPlate.task_id)
            .where(
                AutoTask.work_line_id == work_line_id,
                AutoOutboundPlate.pick_command_code.is_not(None),
                AutoOutboundPlate.put_consumed_at_ms.is_(None),
                AutoOutboundPlate.empty_observation.is_(None),
            )
        )
        for original in outstanding:
            put = (original.source_arrival_snapshot or {}).get("put_command", {})
            if put.get("params", {}).get("source") == SCAN and await self._result(
                session, original, "put"
            ):
                continue
            if await self._result(session, original, "pick", empty=True):
                continue
            return False
        return True

    async def advance(self, session, *, work_line_id, plate_id, now_ms):
        bindings = await lock_line(session, self.dispatch, work_line_id)
        plate = await session.get(AutoOutboundPlate, plate_id)
        task = await session.get(AutoTask, plate.task_id) if plate else None
        if task is None or task.work_line_id != work_line_id or task.business_type != "outbound":
            return plate
        line = await session.get(WorkLine, work_line_id)
        settings = self.settings.get(work_line_id)
        enabled = bool(
            line
            and line.is_enabled
            and line.business_code == "smt_auto"
            and line.line_code == task.workline_code
            and settings
            and settings.work_line_version == line.version
        )
        if plate.pick_command_code is None:
            if not enabled:
                return plate
            scanner = bindings.get(line.device_bindings.get("PLATFORM_SCANNER"))
            role = await self._pick_role(session, task, plate, bindings, scanner)
            if role is None or not await self._platform_clear(session, work_line_id):
                return plate
            candidates = await session.scalars(
                select(AutoOutboundPlate).where(
                    AutoOutboundPlate.task_id == task.task_id,
                    AutoOutboundPlate.pending.is_(True),
                    AutoOutboundPlate.pick_command_code.is_(None),
                )
            )
            priority = self._pick_priority(plate)
            for candidate in candidates:
                if self._pick_priority(candidate) < priority and await self._pick_role(
                    session, task, candidate, bindings, scanner
                ):
                    return plate
            params = plate.source_params
            if plate.bin_work_id:
                source = dict(
                    location_id="STATION_WORK1",
                    location_type="BIN",
                    bin_type=str(params["bin_type"]),
                    bin_cell_location=params["cell_index"],
                    reel_layer=params["reel_layer"],
                    reel_thickness=params["reel_thickness"],
                    reel_diameter=params["reel_diameter"],
                    reel_totalthickness=float(
                        Decimal(str(params["reel_thickness"])) * params["reel_layer"]
                    ),
                )
            else:
                source = dict(
                    location_id="STATION_B",
                    location_type="FIVE_LAYER_RACK",
                    **{k: plate.source_locator[k] for k in ("rack_id", "rack_face")},
                    rack_layer=str(params["rack_layer"]),
                    rack_column=str(params["rack_column"]),
                    reel_thickness=params["reel_thickness"],
                    reel_diameter=params["reel_diameter"],
                )
            command = PickAndPut.from_millimeters(source=source, target=SCAN)
            timeout = (
                settings.bin_arm_timeout_ms if plate.bin_work_id else settings.rack_arm_timeout_ms
            )
            view = await self.ecs.create(
                session,
                owner=OWNER,
                call_key=plate.pick_call_key,
                device_code=role.device_code,
                timeout_ms=timeout,
                command=command,
            )
            plate.pick_command_code = view.command_code
            original = dict(
                device_code=role.device_code, timeout_ms=timeout, params=command.model_dump()
            )
            if plate.bin_work_id is not None:
                original["platform_scanner_id"] = scanner.device_id
            plate.source_arrival_snapshot = {
                **plate.source_arrival_snapshot,
                "pick_command": original,
            }
        await observe_arm_result(self.ecs, session, plate, "pick", owner=OWNER)
        scan = await self._pick_scan(session, plate)
        if scan is None:
            empty = await self._result(session, plate, "pick", empty=True)
            if empty is not None and await self._grant(session, plate):
                await self._empty(session, task, plate, empty, now_ms, allow_continue=enabled)
            return plate
        if plate.pick_consumed_at_ms is None:
            plate.pick_consumed_at_ms = now_ms
            plate.barcode, plate.scanned_at_ms = scan.barcode, scan.scanned_at
        context = {k: v for k, v in plate.source_locator.items() if k != "type"}
        context.update(task_id=plate.task_id, barcode=scan.barcode, scanned_at=scan.scanned_at)
        operation = (
            "outbound.bin.material.decide@v1"
            if plate.bin_work_id
            else "outbound.rackslot.material.decide@v1"
        )
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.plate_id == plate.id,
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE",
                )
                .order_by(AutoOutboundDecisionRound.round_no)
            )
        )
        current = rounds[-1] if rounds else None
        previous = None
        for index, item in enumerate(rounds):
            if (
                item.task_id != task.task_id
                or item.round_no != index + 1
                or item.previous_round_id != (previous.id if previous else None)
                or item.request_data != context
                or item.operation != operation
                or item.stage_key != f"plate:{plate.id}:material"
            ):
                return plate
            if previous:
                original = await read_decision(self.requests, session, previous)
                wait = (previous.response_snapshot or {}).get("data", {})
                if (
                    original is None
                    or original.response_data != wait
                    or wait.get("result") != "WAIT"
                    or previous.unresolved
                    or previous.next_eligible_at_ms
                    != previous.consumed_at_ms + wait.get("retry_after_ms", 0)
                ):
                    return plate
            previous = item
        retry = False
        if current:
            result = await read_decision(self.requests, session, current)
            if result is None:
                return plate
            data = result.response_data
            try:
                DECISIONS["bin" if plate.bin_work_id else "rack"].validate_python(
                    data, extra="ignore"
                )
            except (ValueError, TypeError):
                return plate
            snapshot = dict(
                http_status=200, code="DECIDED", timestamp=result.response_timestamp, data=data
            )
            if not current.unresolved and current.response_snapshot != snapshot:
                return plate
            if data["result"] == "WAIT":
                if current.unresolved:
                    current.response_snapshot = snapshot
                    current.response_recorded_at_ms = current.consumed_at_ms = (
                        current.wait_recorded_at_ms
                    ) = now_ms
                    current.retry_after_ms = data["retry_after_ms"]
                    current.next_eligible_at_ms = now_ms + current.retry_after_ms
                    current.unresolved = False
                    return plate
                if (
                    current.next_eligible_at_ms != current.consumed_at_ms + data["retry_after_ms"]
                    or now_ms < current.next_eligible_at_ms
                    or plate.final_decision_round_id is not None
                ):
                    return plate
                retry = True
        if current is None or retry:
            identity = str(uuid7())
            model = BinDecide if plate.bin_work_id else RackDecide
            ref = await self.requests.send(session, model(**context), operation_id=identity)
            session.add(
                AutoOutboundDecisionRound(
                    task_id=task.task_id,
                    plate_id=plate.id,
                    purpose="MATERIAL_DECIDE",
                    stage_key=f"plate:{plate.id}:material",
                    round_no=current.round_no + 1 if current else 1,
                    previous_round_id=current.id if current else None,
                    operation=operation,
                    operation_id=identity,
                    message_id=ref.message_id,
                    request_data=context,
                    unresolved=True,
                )
            )
            await session.flush()
            return plate
        if plate.final_decision_round_id not in (None, current.id):
            return plate
        is_ng = data["result"] == "REJECT"
        target = data["ng_locator" if is_ng else "target_locator"]
        if current.unresolved:
            current.response_snapshot = snapshot
            current.response_recorded_at_ms = current.consumed_at_ms = now_ms
            current.unresolved = False
            plate.final_decision_round_id = current.id
            plate.target_snapshot = target
            plate.next_source_action = None if is_ng else data.get("next_source_action")
        if plate.target_snapshot != target:
            return plate
        if not is_ng:
            await self._next(session, plate, data)
        if plate.put_command_code is None:
            role_name = "BIN_ARM" if is_ng else "RACK_ARM"
            role = next((b for b in bindings.values() if b and b.device_role == role_name), None)
            if (
                not enabled
                or role is None
                or (not is_ng and not await self._target_ready(session, task, plate, bindings))
            ):
                return plate
            if is_ng:
                params = dict(source=SCAN, target=NG)
                timeout_ms = settings.bin_arm_timeout_ms
            else:
                params = dict(
                    source=SCAN,
                    target=dict(
                        location_id="STATION_A",
                        location_type="FIVE_LAYER_RACK",
                        **{k: target[k] for k in ("rack_id", "rack_face")},
                        rack_layer=str(target["rack_layer"]),
                        rack_column=str(target["rack_column"]),
                        **{k: plate.source_params[k] for k in ("reel_thickness", "reel_diameter")},
                    ),
                )
                timeout_ms = settings.rack_arm_timeout_ms
            plate.put_call_key = f"auto:{task.task_id}:outbound:{plate.id}:put"
            command = PickAndPut.from_millimeters(**params)
            params = command.model_dump()
            view = await self.ecs.create(
                session,
                owner=OWNER,
                call_key=plate.put_call_key,
                device_code=role.device_code,
                timeout_ms=timeout_ms,
                command=command,
            )
            plate.put_command_code = view.command_code
            # Keep the exact target locator available to the public rack authority reader.
            plate.source_arrival_snapshot = {
                **plate.source_arrival_snapshot,
                "put_command": dict(
                    device_code=role.device_code,
                    timeout_ms=timeout_ms,
                    params=params,
                ),
            }
        await observe_arm_result(self.ecs, session, plate, "put", owner=OWNER)
        put = await self._result(session, plate, "put")
        if put is None:
            return plate
        plate.put_consumed_at_ms = plate.put_consumed_at_ms or now_ms
        if plate.report_message_id is None:
            report = dict(
                task_id=task.task_id,
                source_locator=plate.source_locator,
                barcode=scan.barcode,
                to_locator={k: target[k] for k in ("type", "zone_code")}
                if is_ng
                else {k: target[k] for k in ("type", "rack_id", "rack_face", "slot_id")},
                occurred_at=put.finish_time,
            )
            plate.report_operation_id = str(uuid7())
            ref = await self.requests.send(
                session, MovementReport(**report), operation_id=plate.report_operation_id
            )
            plate.report_message_id, plate.report_data = ref.message_id, report
        if is_ng and enabled:
            round_ = await self._continue_exception(session, task, plate, now_ms, source_done=False)
            if round_ is not None:
                plate.pending = False
        await session.flush()
        return plate

    @staticmethod
    def _arrival_context(plate):
        return {
            k: v
            for k, v in plate.source_arrival_snapshot.items()
            if k not in ("pick_command", "put_command")
        }

    async def _empty_context(self, session, plate, result):
        if plate.barcode is not None:
            # A scan for this pick conflicts with an empty observation.
            return None
        context = dict(
            task_id=plate.task_id,
            source_locator=plate.source_locator,
            observed_at=result.finish_time,
        )
        if plate.source_member_id is not None:
            member = await session.get(AutoOutboundSourceMember, plate.source_member_id)
            if (
                member is None
                or member.task_id != plate.task_id
                or member.preparation_action_id != plate.source_preparation_action_id
            ):
                return None
            context["plan_revision"] = member.plan_revision
        return context

    async def _empty_rounds(self, session, plate, context):
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.plate_id == plate.id,
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE",
                )
                .order_by(AutoOutboundDecisionRound.round_no)
            )
        )
        previous = None
        for index, round_ in enumerate(rounds, 1):
            if (
                round_.task_id != plate.task_id
                or round_.operation != EMPTY
                or round_.stage_key != f"plate:{plate.id}:empty"
                or round_.request_data != context
                or round_.round_no != index
                or round_.previous_round_id != (previous.id if previous else None)
            ):
                return None
            if previous and not await self._recorded_wait(session, previous):
                return None
            previous = round_
        return rounds

    async def _recorded_wait(self, session, round_):
        response = await read_decision(self.requests, session, round_)
        snapshot = round_.response_snapshot or {}
        data = snapshot.get("data", {})
        if (
            round_.unresolved
            or round_.consumed_at_ms is None
            or round_.wait_recorded_at_ms != round_.consumed_at_ms
            or response is None
            or response.response_data != data
            or snapshot.get("timestamp") != response.response_timestamp
        ):
            return False
        try:
            wait = Wait.model_validate(data)
        except (ValueError, TypeError):
            return False
        return (
            round_.retry_after_ms == wait.retry_after_ms
            and round_.next_eligible_at_ms == round_.consumed_at_ms + wait.retry_after_ms
        )

    async def _empty_closed(self, session, plate):
        if plate.empty_observation is None or plate.final_decision_round_id is None:
            return None
        result = await self._result(session, plate, "pick", empty=True)
        context = await self._empty_context(session, plate, result) if result else None
        if context is None or context != plate.empty_observation:
            return None
        rounds = await self._empty_rounds(session, plate, context)
        if not rounds:
            return None
        round_ = rounds[-1]
        if (
            round_.unresolved
            or round_.consumed_at_ms is None
            or round_.id != plate.final_decision_round_id
            or plate.physical_closure_basis != self._empty_basis(plate, round_)
        ):
            return None
        response = await read_decision(self.requests, session, round_)
        data = (round_.response_snapshot or {}).get("data")
        if response is None or response.response_data != data:
            return None
        if data == {"result": "RETRY"}:
            return "RETRY"
        if data == {"result": "SOURCE_DONE"} and plate.source_closed_basis == {
            "kind": "SOURCE_DONE",
            "round_id": round_.id,
        }:
            return "SOURCE_DONE"
        return None

    @staticmethod
    def _empty_basis(plate, round_):
        return dict(kind="EMPTY", command_code=plate.pick_command_code, round_id=round_.id)

    async def _empty(self, session, task, plate, result, now_ms, *, allow_continue):
        from .request.outbound_source_empty_decide_v1 import EmptyDecide

        context = await self._empty_context(session, plate, result)
        if context is None or plate.empty_observation not in (None, context):
            return
        rounds = await self._empty_rounds(session, plate, context)
        if rounds is None:
            return
        plate.empty_observation = context
        plate.pick_consumed_at_ms = plate.pick_consumed_at_ms or now_ms
        round_ = rounds[-1] if rounds else None
        if round_ is not None:
            if not round_.unresolved:
                if await self._empty_closed(session, plate) == "SOURCE_DONE":
                    if allow_continue and await self._continue_exception(
                        session, task, plate, now_ms, source_done=True
                    ):
                        plate.pending = False
                    return
                if (
                    not await self._recorded_wait(session, round_)
                    or now_ms < round_.next_eligible_at_ms
                ):
                    return
            else:
                response = await read_decision(self.requests, session, round_)
                if response is None:
                    return
                data = response.response_data
                try:
                    decision = EMPTY_DECISION.validate_python(data)
                except (ValueError, TypeError):
                    return
                round_.response_snapshot = dict(
                    http_status=200,
                    code="DECIDED",
                    timestamp=response.response_timestamp,
                    data=data,
                )
                round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
                round_.unresolved = False
                if decision.result == "WAIT":
                    round_.wait_recorded_at_ms = now_ms
                    round_.retry_after_ms = decision.retry_after_ms
                    round_.next_eligible_at_ms = now_ms + decision.retry_after_ms
                else:
                    plate.final_decision_round_id = round_.id
                    plate.physical_closure_basis = self._empty_basis(plate, round_)
                    if decision.result == "RETRY":
                        session.add(
                            AutoOutboundPlate(
                                task_id=plate.task_id,
                                bin_work_id=plate.bin_work_id,
                                source_member_id=plate.source_member_id,
                                source_rack_job_id=plate.source_rack_job_id,
                                source_preparation_action_id=plate.source_preparation_action_id,
                                grant_kind="RETRY",
                                grant_round_id=round_.id,
                                grant_snapshot=data,
                                source_locator=plate.source_locator,
                                source_params=plate.source_params,
                                source_arrival_snapshot=self._arrival_context(plate),
                                pick_call_key=f"auto:{plate.task_id}:outbound:retry:{round_.id}",
                                pending=True,
                            )
                        )
                        plate.pending = False
                    else:
                        plate.source_closed_basis = dict(kind="SOURCE_DONE", round_id=round_.id)
                        if allow_continue and await self._continue_exception(
                            session, task, plate, now_ms, source_done=True
                        ):
                            plate.pending = False
                await session.flush()
                return
        operation_id = str(uuid7())
        ref = await self.requests.send(
            session, EmptyDecide.model_validate(context), operation_id=operation_id
        )
        session.add(
            AutoOutboundDecisionRound(
                task_id=plate.task_id,
                purpose="EMPTY_DECIDE",
                plate_id=plate.id,
                stage_key=f"plate:{plate.id}:empty",
                round_no=len(rounds) + 1,
                previous_round_id=round_.id if round_ else None,
                operation=EMPTY,
                operation_id=operation_id,
                message_id=ref.message_id,
                request_data=context,
                unresolved=True,
            )
        )
        await session.flush()

    async def _exception_round(self, session, plate):
        model = AutoOutboundDecisionRound
        return await session.scalar(
            select(model).where(
                model.task_id == plate.task_id,
                model.purpose == "WORK_PLAN",
                model.bin_work_id == plate.bin_work_id,
                model.source_preparation_action_id == plate.source_preparation_action_id,
                model.trigger_plate_id == plate.id,
            )
        )

    async def _continue_exception(self, session, task, plate, now_ms, *, source_done):
        if source_done and await self._empty_closed(session, plate) != "SOURCE_DONE":
            return None
        if not source_done and not await self.closed(session, plate):
            return None
        existing = await self._exception_round(session, plate)
        if existing is not None:
            return existing
        if task.status != "EXECUTING" and not (
            task.status == "CANCELLED" and plate.bin_work_id is not None
        ):
            return None
        bindings = await lock_line(session, self.dispatch, task.work_line_id)
        if not await self._source_ready(session, task, plate, bindings):
            return None
        others = [
            p
            for p in await self._subject_plates(
                session, bin_work_id=plate.bin_work_id, member_id=plate.source_member_id
            )
            if p.id != plate.id
        ]
        for other in others:
            if await self.closed(session, other):
                continue
            decision = await self._decision(session, other)
            if decision is None or (
                "next_source_action" in decision[0]
                and not any(p.grant_parent_plate_id == other.id for p in (*others, plate))
            ):
                return None
        common = dict(work_line_id=task.work_line_id, now_ms=now_ms, trigger_plate_id=plate.id)
        if plate.bin_work_id is not None:
            work = await session.get(AutoBinWork, plate.bin_work_id)
            round_ = await self.work_plans.start_bin(session, arrival_id=work.arrival_id, **common)
        else:
            round_ = await self.work_plans.start_return_face(
                session, member_id=plate.source_member_id, **common
            )
        return round_ if round_ is not None and round_.trigger_plate_id == plate.id else None

    async def _next(self, session, plate, data):
        successor = data.get("next_source_action")
        if successor is None or "result" in successor:
            return
        try:
            plan = PLANS["bin" if plate.bin_work_id else "rack"].validate_python(
                {"result": "READY", **successor}, extra="ignore"
            )
        except (ValueError, TypeError):
            return
        if not plate.bin_work_id and any(
            successor[k] != plate.source_locator[k] for k in ("rack_id", "rack_face")
        ):
            return
        existing = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.grant_parent_plate_id == plate.id)
        )
        if existing:
            return
        locator = (
            {
                "type": "BIN_CELL",
                "bin_code": plate.source_locator["bin_code"],
                "cell_id": successor["cell_id"],
            }
            if plate.bin_work_id
            else {
                "type": "RACK_SLOT",
                **{k: successor[k] for k in ("rack_id", "rack_face", "slot_id")},
            }
        )
        if await outbound_locator_closed(
            session,
            task_id=plate.task_id,
            bin_work_id=plate.bin_work_id,
            source_member_id=plate.source_member_id,
            locator=locator,
        ):
            return
        session.add(
            AutoOutboundPlate(
                task_id=plate.task_id,
                bin_work_id=plate.bin_work_id,
                source_member_id=plate.source_member_id,
                source_rack_job_id=plate.source_rack_job_id,
                source_preparation_action_id=plate.source_preparation_action_id,
                grant_kind="NEXT",
                grant_parent_plate_id=plate.id,
                grant_snapshot=successor,
                source_locator=locator,
                source_params={
                    k: successor[k]
                    for k in type(plan).model_fields
                    if k != "result" and k not in locator
                },
                source_arrival_snapshot=self._arrival_context(plate),
                pick_call_key=f"auto:{plate.task_id}:outbound:{plate.id}:next",
                pending=True,
            )
        )
        await session.flush()

    async def _final_material(self, session, plate):
        if plate.final_decision_round_id is None or not await self._grant(session, plate):
            return None
        round_ = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
        if (
            round_ is None
            or round_.task_id != plate.task_id
            or round_.plate_id != plate.id
            or round_.purpose != "MATERIAL_DECIDE"
            or round_.operation
            != (
                "outbound.bin.material.decide@v1"
                if plate.bin_work_id
                else "outbound.rackslot.material.decide@v1"
            )
            or round_.consumed_at_ms is None
            or round_.unresolved
        ):
            return None
        response = await read_decision(self.requests, session, round_)
        data = response.response_data if response else None
        if (
            not isinstance(data, dict)
            or data != (round_.response_snapshot or {}).get("data")
            or data.get("result") not in ("ACCEPT", "REJECT")
            or data.get("ng_locator" if data["result"] == "REJECT" else "target_locator")
            != plate.target_snapshot
        ):
            return None
        try:
            DECISIONS["bin" if plate.bin_work_id else "rack"].validate_python(data, extra="ignore")
        except (ValueError, TypeError):
            return None
        scan = await self._pick_scan(session, plate)
        if scan is None:
            return None
        expected = {k: v for k, v in plate.source_locator.items() if k != "type"}
        expected.update(task_id=plate.task_id, barcode=scan.barcode, scanned_at=scan.scanned_at)
        if round_.request_data != expected:
            return None
        chain = list(
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.plate_id == plate.id,
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE",
                )
                .order_by(AutoOutboundDecisionRound.round_no)
            )
        )
        previous = None
        for index, item in enumerate(chain):
            if (
                item.round_no != index + 1
                or item.task_id != plate.task_id
                or item.request_data != expected
                or item.operation != round_.operation
                or item.previous_round_id != (previous.id if previous else None)
            ):
                return None
            if previous:
                response = await read_decision(self.requests, session, previous)
                wait = (previous.response_snapshot or {}).get("data", {})
                if (
                    previous.unresolved
                    or previous.consumed_at_ms is None
                    or response is None
                    or response.response_data != wait
                    or wait.get("result") != "WAIT"
                    or previous.next_eligible_at_ms
                    != previous.consumed_at_ms + wait.get("retry_after_ms", 0)
                ):
                    return None
            previous = item
        if not chain or chain[-1].id != round_.id:
            return None
        return data, scan

    async def _decision(self, session, plate):
        decision = await self._final_material(session, plate)
        return decision if decision and decision[0]["result"] == "ACCEPT" else None

    async def _expected_report(self, session, plate):
        decision = await self._final_material(session, plate)
        put = await self._result(session, plate, "put")
        if decision is None or put is None:
            return None
        data, scan = decision
        command = plate.source_arrival_snapshot.get("put_command", {}).get("params", {})
        if data["result"] == "REJECT":
            if command != dict(source=SCAN, target=NG):
                return None
            return dict(
                task_id=plate.task_id,
                source_locator=plate.source_locator,
                barcode=scan.barcode,
                to_locator={k: data["ng_locator"][k] for k in ("type", "zone_code")},
                occurred_at=put.finish_time,
            )
        target = data["target_locator"]
        job = (
            await self.dispatch.progress(session, job_id=plate.target_rack_job_id)
            if plate.target_rack_job_id
            else None
        )
        action = (
            next((a for a in job.actions if a.id == plate.target_preparation_action_id), None)
            if job
            else None
        )
        point = {"kind": "RACK_POSITION", "location_code": job.origin.point} if job else None
        if (
            job is None
            or job.origin.task_id != plate.task_id
            or job.origin.rack_id != target["rack_id"]
            or action is None
            or action.kind not in ("ENTER", "ROTATE")
            or action.phase != "APPLIED"
            or not action.outcome
            or action.outcome.get("status") != "SUCCEEDED"
            or action.actual_position != point
            or action.actual_face != target["rack_face"]
            or action.target_face != target["rack_face"]
        ):
            return None
        expected = dict(
            source=SCAN,
            target=dict(
                location_id="STATION_A",
                location_type="FIVE_LAYER_RACK",
                **{k: target[k] for k in ("rack_id", "rack_face")},
                rack_layer=str(target["rack_layer"]),
                rack_column=str(target["rack_column"]),
                **{k: plate.source_params[k] for k in ("reel_thickness", "reel_diameter")},
            ),
        )
        # Frozen commands using rack_face may predate the diameter conversion as well.
        legacy_inches = {
            **expected,
            "target": {
                **expected["target"],
                "reel_diameter": ecs_reel_diameter(expected["target"]["reel_diameter"]),
            },
        }
        if command not in (expected, legacy_inches):
            try:
                normalized = PickAndPut.from_millimeters(**expected).model_dump()
            except ValidationError:
                return None
            if command != normalized:
                return None
        return dict(
            task_id=plate.task_id,
            source_locator=plate.source_locator,
            barcode=scan.barcode,
            to_locator={k: target[k] for k in ("type", "rack_id", "rack_face", "slot_id")},
            occurred_at=put.finish_time,
        )

    async def _report_accepted(self, session, plate):
        expected = await self._expected_report(session, plate)
        if expected is None or plate.report_message_id is None or plate.report_data != expected:
            return False
        report = await self.requests.get_result(
            session,
            message_id=plate.report_message_id,
            operation=REPORT,
            operation_id=plate.report_operation_id,
        )
        return (
            (report.message_id, report.operation, report.operation_id)
            == (plate.report_message_id, REPORT, plate.report_operation_id)
            and report.availability == "FOUND"
            and report.response_state == "VALID"
            and report.http_status == 200
            and report.response_code in ("RECORDED", "DUPLICATE")
            and report.response_data == {}
        )

    async def closed(self, session, plate):
        if plate.empty_observation is not None:
            return (
                await self._grant(session, plate)
                and await self._empty_closed(session, plate) is not None
            )
        if (
            plate.report_consumed_at_ms is None
            or plate.report_response is None
            or not await self._report_accepted(session, plate)
        ):
            return False
        report = await self.requests.get_result(
            session,
            message_id=plate.report_message_id,
            operation=REPORT,
            operation_id=plate.report_operation_id,
        )
        return plate.report_response == dict(
            http_status=200, code=report.response_code, timestamp=report.response_timestamp, data={}
        )

    async def advance_report(self, session, *, plate_id, now_ms):
        plate = await session.get(AutoOutboundPlate, plate_id, with_for_update=True)
        if plate is None or not await self._report_accepted(session, plate):
            return plate
        report = await self.requests.get_result(
            session,
            message_id=plate.report_message_id,
            operation=REPORT,
            operation_id=plate.report_operation_id,
        )
        plate.report_response = dict(
            http_status=200, code=report.response_code, timestamp=report.response_timestamp, data={}
        )
        plate.report_consumed_at_ms = plate.report_consumed_at_ms or now_ms
        plate.direct_obligations_closed_at_ms = plate.direct_obligations_closed_at_ms or now_ms
        if plate.report_data["to_locator"]["type"] != "NG_ZONE":
            plate.pending = False
        else:
            final = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
            if final.response_snapshot["data"]["source_disposition"] == "CLOSE":
                plate.source_closed_basis = dict(kind="NG_CLOSE", round_id=final.id)
        return plate

    async def _subject_plates(self, session, *, bin_work_id=None, member_id=None):
        query = (
            select(AutoOutboundPlate).where(AutoOutboundPlate.bin_work_id == bin_work_id)
            if bin_work_id
            else select(AutoOutboundPlate).where(AutoOutboundPlate.source_member_id == member_id)
        )
        return list(await session.scalars(query))

    async def _subject_closed(
        self, session, *, bin_work_id=None, member_id=None, reports=True, for_release=False
    ):
        plates = await self._subject_plates(session, bin_work_id=bin_work_id, member_id=member_id)
        subject = (
            await session.get(AutoBinWork, bin_work_id)
            if bin_work_id is not None
            else await session.get(AutoOutboundSourceMember, member_id)
        )
        if subject is None:
            return False
        terminals = 0
        accepted = []
        for plate in plates:
            if plate.task_id != subject.task_id:
                return False
            decision = await self._decision(session, plate)
            if decision is not None:
                accepted.append(plate)
                if "next_source_action" not in decision[0]:
                    terminals += 1
                elif not any(p.grant_parent_plate_id == plate.id for p in plates):
                    return False
                continue
            if not await self.closed(session, plate):
                return False
            if await self._empty_closed(session, plate) == "RETRY":
                if not any(p.grant_round_id == plate.final_decision_round_id for p in plates):
                    return False
            elif await self._exception_round(session, plate) is None:
                return False
        if terminals + int(await self._subject_no_work(session, subject)) != 1:
            return False
        # A terminal ACCEPT ends the source BIN's work. Delivery still owns its reports.
        if reports and not (for_release and terminals == 1):
            for plate in accepted:
                if not await self.closed(session, plate):
                    return False
        return True

    async def bin_closed(self, session, work, *, for_release=False):
        return work.business_type == "outbound" and await self._subject_closed(
            session, bin_work_id=work.id, for_release=for_release
        )

    async def face_closed(self, session, member):
        return await self._subject_closed(session, member_id=member.id)

    async def face_source_terminal(self, session, member):
        return await self._subject_closed(session, member_id=member.id, reports=False)

    async def _subject_no_work(self, session, subject):
        basis = subject.terminal_basis or {}
        if basis.get("kind") != "NO_WORK":
            return False
        round_ = await session.get(AutoOutboundDecisionRound, basis.get("round_id"))
        is_bin = isinstance(subject, AutoBinWork)
        operation = "outbound.bin.work_plan@v1" if is_bin else "outbound.return_rack.work_plan@v1"
        request = (
            dict(task_id=subject.task_id, bin_code=subject.bin_code, scanned_at=subject.scanned_at)
            if is_bin
            else dict(
                task_id=subject.task_id,
                rack_id=subject.rack_id,
                rack_face=subject.rack_face,
                arrived_at=subject.arrived_at_ms,
            )
        )
        if (
            round_ is None
            or round_.task_id != subject.task_id
            or round_.purpose != "WORK_PLAN"
            or round_.bin_work_id != (subject.id if is_bin else None)
            or round_.source_preparation_action_id
            != (None if is_bin else subject.preparation_action_id)
            or round_.unresolved
            or round_.consumed_at_ms is None
            or round_.operation != operation
            or round_.operation_id != basis.get("operation_id")
            or round_.request_data != request
        ):
            return False
        response = await read_decision(self.requests, session, round_)
        return bool(
            response
            and response.response_data == {"result": "NO_WORK"}
            and round_.response_snapshot
            == dict(
                http_status=200,
                code="DECIDED",
                timestamp=response.response_timestamp,
                data=response.response_data,
            )
        )

    async def source_clear(self, session, *, job_id):
        plates = list(
            await session.scalars(
                select(AutoOutboundPlate).where(AutoOutboundPlate.source_rack_job_id == job_id)
            )
        )
        for plate in plates:
            if await self._pick_scan(session, plate) is None and not (
                plate.empty_observation is not None and await self.closed(session, plate)
            ):
                return False
        return True

    async def target_clear(self, session, *, job_id):
        job = await self.dispatch.progress(session, job_id=job_id)
        if job is None:
            return False
        plates = list(
            await session.scalars(
                select(AutoOutboundPlate).where(AutoOutboundPlate.task_id == job.origin.task_id)
            )
        )
        for plate in plates:
            if plate.target_rack_job_id == job_id and plate.put_call_key is not None:
                if await self._result(session, plate, "put") is None:
                    return False
            elif (
                (plate.target_snapshot or {}).get("rack_id") == job.origin.rack_id
                and plate.final_decision_round_id is not None
                and plate.put_call_key is None
            ):
                final = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
                data = (final.response_snapshot or {}).get("data", {}) if final else {}
                if data.get("result") == "ACCEPT":
                    if (
                        data.get("target_preparation") is None
                        and job.origin.source_ref == f"PLATE_TARGET:{plate.id}"
                        and plate.target_rack_job_id == job_id
                        and plate.target_preparation_action_id is None
                        and job.position_action_id is None
                    ):
                        continue
                    own = next(
                        (
                            a
                            for a in job.actions
                            if a.id == plate.target_preparation_action_id
                            and a.source_ref == f"PLATE_TARGET:{plate.id}"
                        ),
                        None,
                    )
                    if (
                        data.get("target_preparation") not in ("ROTATE", "REPLACE")
                        or own is not None
                    ):
                        return False
        return True

    async def target_reports_closed(self, session, *, job_id):
        plates = await session.scalars(
            select(AutoOutboundPlate).where(AutoOutboundPlate.target_rack_job_id == job_id)
        )
        for plate in plates:
            if plate.report_consumed_at_ms is None or not await self.closed(session, plate):
                return False
        return True

    async def target_demand(self, session, *, job, rows):
        """Only original, fully covered production demand may release a TRANSFER rack."""
        task = await session.get(AutoTask, job.origin.task_id)
        inputs = [r for r in rows[AutoTaskInput] if r.task_id == task.task_id]
        members = [m for m in rows[AutoOutboundSourceMember] if m.task_id == task.task_id]
        supplies = [m for m in rows[AutoSupplyMember] if m.task_id == task.task_id]
        plates = [p for p in rows[AutoOutboundPlate] if p.task_id == task.task_id]
        rounds = [r for r in rows[AutoOutboundDecisionRound] if r.task_id == task.task_id]
        bins = [b for b in rows[AutoBinWork] if b.task_id == task.task_id]
        expected_members = {
            (r.id, r.applied_plan_revision, index, rack["rack_id"], face)
            for r in inputs
            if r.applied_plan_revision is not None
            for index, rack in enumerate(r.effective_request.get("added_return_racks", []))
            for face in rack["rack_faces"]
        }
        actual_members = {
            (m.plan_input_id, m.plan_revision, m.rack_array_index, m.rack_id, m.rack_face)
            for m in members
        }
        expected_supply = {
            (
                r.id,
                r.applied_plan_revision,
                index,
                face_index,
                rack["rack_id"],
                face,
                r.operation_id,
            )
            for r in inputs
            if r.applied_plan_revision is not None
            for index, rack in enumerate(r.effective_request.get("added_bin_source_racks", []))
            for face_index, face in enumerate(rack["rack_face"])
        }
        actual_supply = {
            (
                m.plan_input_id,
                m.plan_revision,
                m.rack_array_index,
                m.face_array_index,
                m.rack_id,
                m.rack_face,
                m.supply_change_id,
            )
            for m in supplies
        }
        if (
            expected_members != actual_members
            or len(actual_members) != len(members)
            or expected_supply != actual_supply
            or len(actual_supply) != len(supplies)
        ):
            return "UNKNOWN"
        if task.status == "COMPLETED" and task.closed_at_ms is not None:
            from .task_flow import TaskFlow

            proof = await TaskFlow(
                self.requests, roller=self.roller, dispatch=self.dispatch, outbound=self
            ).retention_basis(session, task, now_ms=task.closed_at_ms)
            if proof is not None and await self.target_clear(session, job_id=job.id):
                return "NONE"
        plate_ids = {p.id for p in plates}
        bin_ids = {b.id for b in bins}
        face_actions = {m.preparation_action_id for m in members}
        decisions = {}
        for plate in plates:
            decision = await self._decision(session, plate)
            if decision is not None:
                decisions[plate.id] = decision[0]
        replacements = [
            p
            for p in plates
            if p.id in decisions
            and decisions[p.id].get("target_preparation") == "REPLACE"
            and decisions[p.id]["target_locator"]["rack_id"] != job.origin.rack_id
            and p.put_call_key is None
        ]
        descendants = set()
        frontier = {p.id for p in replacements}
        while frontier:
            children = {
                p.id
                for p in plates
                if p.grant_parent_plate_id in frontier
                and p.id not in descendants
                and await self._grant(session, p)
            }
            descendants.update(children)
            frontier = children
        if any(
            (r.plate_id not in descendants and (r.unresolved or r.consumed_at_ms is None))
            or (r.plate_id is not None and r.plate_id not in plate_ids)
            or (
                r.purpose == "WORK_PLAN"
                and r.bin_work_id not in bin_ids
                and r.source_preparation_action_id not in face_actions
            )
            for r in rounds
        ):
            return "CONTINUE"
        if any(p.id not in decisions and p.id not in descendants for p in plates):
            return "CONTINUE"
        if replacements:
            for plate in plates:
                decision = decisions.get(plate.id)
                if (
                    decision
                    and decision["target_locator"]["rack_id"] == job.origin.rack_id
                    and await self._result(session, plate, "put") is None
                ):
                    return "CONTINUE"
            return "NONE" if await self.target_clear(session, job_id=job.id) else "CONTINUE"
        if not bins and not members:
            return "CONTINUE"
        if supplies:
            if self.roller is None:
                return "UNKNOWN"
            progress = await self.roller.get_supply_progress(
                session, work_line_id=task.work_line_id, task_id=task.task_id
            )
            expected = {(m.plan_revision, m.rack_id, m.rack_face) for m in supplies}
            if {
                (p.source.plan_revision, p.source.rack_id, p.source.rack_face) for p in progress
            } != expected or len(progress) != len(expected):
                return "UNKNOWN"
            from .task_flow import supply_member_closed

            for member in supplies:
                facts = [
                    p
                    for p in progress
                    if (p.source.plan_revision, p.source.rack_id, p.source.rack_face)
                    == (member.plan_revision, member.rack_id, member.rack_face)
                ]
                if any(p.task_id != task.task_id for p in facts) or not supply_member_closed(
                    member, facts, inputs
                ):
                    return "CONTINUE"
        from .task_flow import withdrawn_outbound_source

        for member in members:
            if not await self.face_source_terminal(
                session, member
            ) and not await withdrawn_outbound_source(
                session,
                dispatch=self.dispatch,
                task=task,
                member=member,
                inputs=inputs,
                plates=plates,
            ):
                return "CONTINUE"
        for work in bins:
            if work.production_hold_reason or work.business_type != "outbound":
                return "CONTINUE"
            if any(p.bin_work_id == work.id for p in plates):
                if not await self._subject_closed(session, bin_work_id=work.id, reports=False):
                    return "CONTINUE"
            else:
                # Reuse the common strict original NO_WORK chain; no plate/report branch applies.
                from .task_flow import TaskFlow

                if not await TaskFlow(
                    self.requests, roller=self.roller, dispatch=self.dispatch
                )._bin_terminal(session, work):
                    return "CONTINUE"
        if any([await self._result(session, plate, "put") is None for plate in plates]):
            return "CONTINUE"
        return "NONE"
