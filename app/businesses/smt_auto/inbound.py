"""Normal inbound original intents; caller owns the ordered SERIALIZABLE transaction."""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.ecs_command import CommandOwner
from app.integrations.ecs_command.contracts import ResultEnvelope
from app.models import WorkLine

from ._execution_facts import arrival_is_open, observe_arm_result, read_decision
from .ecs_command import ArmResult, PickAndPut
from .inbound_contracts import Reject, Retry, Scan, SourceAbsent, Target
from .locking import lock_line
from .models import (
    AutoBinWork,
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoTask,
    AutoTaskInput,
)
from .request.putaway_material_decide_v1 import MaterialDecide
from .request.putaway_material_movement_report_v1 import MovementReport
from .request.putaway_source_empty_decide_v1 import EmptyDecide
from .work_plan_contracts import DECISIONS, InboundSource, Wait
from .work_plans import WorkPlans

OWNER = CommandOwner("smt_auto")
REPORT = "putaway.material.movement_report@v1"
DECIDE = "putaway.material.decide@v1"
SCAN = {"location_id": "STATION_SCAN", "location_type": "SCAN_PLATFORM"}


@dataclass(frozen=True)
class PhysicalSettings:
    work_line_id: int
    work_line_version: int
    rack_arm_timeout_ms: int
    bin_arm_timeout_ms: int
    source_locations: tuple[tuple[str, str], ...]

    def __post_init__(self):
        if any(
            type(value) is not int or not minimum <= value <= 2**63 - 1
            for value, minimum in (
                (self.work_line_id, 1),
                (self.work_line_version, 1),
                (self.rack_arm_timeout_ms, 0),
                (self.bin_arm_timeout_ms, 0),
            )
        ):
            raise ValueError("invalid AUTO physical configuration")
        if len(dict(self.source_locations)) != len(self.source_locations) or any(
            station not in ("STATION_A", "STATION_B") for _, station in self.source_locations
        ):
            raise ValueError("explicit source station mapping required")


class Inbound:
    def __init__(
        self,
        requests,
        ecs,
        dispatch,
        roller,
        *,
        settings: PhysicalSettings | tuple[PhysicalSettings, ...],
    ):
        configured = (settings,) if isinstance(settings, PhysicalSettings) else settings
        if not isinstance(configured, tuple) or any(
            not isinstance(item, PhysicalSettings) for item in configured
        ):
            raise ValueError("validated physical settings required")
        self.requests, self.ecs, self.dispatch, self.roller = requests, ecs, dispatch, roller
        self.settings = {item.work_line_id: item for item in configured}
        if len(self.settings) != len(configured):
            raise ValueError("duplicate AUTO physical workline settings")
        self.work_plans = WorkPlans(requests, dispatch)
        self.work_plans.inbound = self

    async def accept_source_rack_exhausted(self, ctx, payload):
        from app.integrations.wms_callback import (
            Accepted,
            Conflict,
            Duplicate,
            Rejected,
            Unavailable,
        )

        task = await ctx.session.get(AutoTask, payload.task_id)
        if task is None:
            raise Unavailable()
        if task.business_type != "inbound":
            raise Rejected("INVALID_DATA")
        await lock_line(ctx.session, self.dispatch, task.work_line_id)
        data = payload.model_dump()
        digest = canonical_digest(data)
        original = await ctx.session.scalar(
            select(AutoTaskInput).where(
                AutoTaskInput.operation == ctx.operation,
                AutoTaskInput.operation_id == ctx.operation_id,
            )
        )
        if original is not None:
            if original.effective_digest != digest or original.request_timestamp != ctx.timestamp:
                raise Conflict({"reason_code": "IDEMPOTENCY_CONFLICT"})
            return Duplicate()
        if task.details_cleaned or task.status in ("COMPLETED", "CANCELLED"):
            raise Conflict({"reason_code": "STATE_CONFLICT"})
        matches = []
        for job in await self.dispatch.jobs(
            ctx.session, work_line_id=task.work_line_id, source_kinds=("AUTO_INBOUND_SOURCE",)
        ):
            if job.origin.task_id == task.task_id and job.origin.rack_id == payload.rack_id:
                matches.extend(
                    (job, action)
                    for action in job.actions
                    if action.kind == "ENTER"
                    and action.transport_task_id == payload.transport_task_id
                )
        if len(matches) != 1:
            raise Conflict({"reason_code": "REFERENCE_CONFLICT"})
        job, action = matches[0]
        ctx.session.add(
            AutoTaskInput(
                task_id=task.task_id,
                operation=ctx.operation,
                operation_id=ctx.operation_id,
                request_timestamp=ctx.timestamp,
                first_received_at_ms=ctx.timestamp,
                effective_digest=digest,
                contract_version="v1",
                effective_request=data,
                disposition="APPLIED",
                source_rack_job_id=job.id,
                source_preparation_action_id=action.id,
            )
        )
        await self.dispatch.note_facts_changed(ctx.session, job_ids=(job.id,))
        await ctx.session.flush()
        return Accepted()

    async def _result(self, session, plate, phase):
        code = getattr(plate, f"{phase}_command_code")
        key = getattr(plate, f"{phase}_call_key")
        snapshot = (
            plate.source_arrival_snapshot if phase == "pick" else plate.target_snapshot
        ) or {}
        command = snapshot.get(f"{phase}_command", {})
        if not code or not key or not command:
            return None
        view = await self.ecs.get_by_call(session, owner=OWNER, call_key=key)
        if (
            view is None
            or view.command_code != code
            or view.status != "SUCCEEDED"
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
            if phase == "pick":
                Scan.model_validate(result.data)
        except (ValidationError, TypeError):
            return None
        if (
            result.command_code != code
            or result.device_code != view.device_code
            or result.result != "SUCCESS"
            or data.actual_qty != 1
            or data.pick_and_put_result != "PUT_FINISHED"
        ):
            return None
        return result

    async def _ready_to_pick(self, session, work, plate, grant):
        round_ = grant
        while True:
            if (
                round_.task_id != work.task_id
                or round_.bin_work_id != work.id
                or round_.stage_key != f"bin:{work.id}:first"
                or round_.purpose != "WORK_PLAN"
                or round_.operation != "putaway.work_plan@v1"
                or round_.unresolved
                or round_.consumed_at_ms is None
            ):
                return False
            if round_.trigger_plate_id is not None:
                return await self.work_plans.inbound_trigger(
                    session, work, round_.trigger_plate_id, before_plate_id=plate.id
                )
            if round_.previous_round_id is None:
                return round_.round_no == 1
            previous = await session.get(AutoInboundDecisionRound, round_.previous_round_id)
            if (
                previous is None
                or previous.round_no + 1 != round_.round_no
                or previous.next_eligible_at_ms is None
                or round_.consumed_at_ms < previous.next_eligible_at_ms
            ):
                return False
            response = await read_decision(self.requests, session, previous)
            if response is None or response.response_data != (previous.response_snapshot or {}).get(
                "data"
            ):
                return False
            try:
                wait = Wait.model_validate(response.response_data)
            except ValidationError:
                return False
            if (
                previous.consumed_at_ms is None
                or previous.wait_recorded_at_ms != previous.consumed_at_ms
                or previous.retry_after_ms != wait.retry_after_ms
                or previous.next_eligible_at_ms != previous.consumed_at_ms + wait.retry_after_ms
            ):
                return False
            round_ = previous

    async def advance(self, session, *, work_line_id, plate_id, now_ms):
        bindings = await lock_line(session, self.dispatch, work_line_id)
        plate = await session.get(AutoInboundPlate, plate_id)
        if plate is None:
            return None
        task = await session.get(AutoTask, plate.task_id)
        work = await session.get(AutoBinWork, plate.bin_work_id)
        if (
            task is None
            or work is None
            or task.work_line_id != work_line_id
            or work.work_line_id != work_line_id
            or work.task_id != task.task_id
            or task.business_type != "inbound"
            or work.business_type != "inbound"
        ):
            return plate
        line = await session.get(WorkLine, work_line_id)
        settings = self.settings.get(work_line_id)
        enabled = (
            settings is not None
            and line is not None
            and line.is_enabled
            and line.business_code == "smt_auto"
            and line.line_code == task.workline_code
            and settings.work_line_version == line.version
        )
        if plate.pick_command_code is None:
            if (
                not enabled
                or task.status != "EXECUTING"
                or work.production_hold_reason
                or work.terminal_basis
                or not await arrival_is_open(self.roller, session, work)
            ):
                return plate
            frozen = plate.source_arrival_snapshot.get("source")
            if not isinstance(frozen, dict):
                return plate
            _, proofs = await self.work_plans._inbound_sources(
                session, task, bindings, original=frozen
            )
            proof = (proofs or {}).get(plate.source_locator.get("rack_id"))
            if (
                proof != frozen
                or not proof
                or proof["rack_job_id"] != plate.source_rack_job_id
                or proof["preparation_action_id"] != plate.source_preparation_action_id
            ):
                return plate
            grant = (
                await session.get(AutoInboundDecisionRound, plate.grant_round_id)
                if plate.grant_round_id
                else None
            )
            if plate.grant_kind == "NEXT":
                parent = await session.get(AutoInboundPlate, plate.grant_parent_plate_id)
                grant = (
                    await session.get(AutoInboundDecisionRound, parent.final_decision_round_id)
                    if parent
                    else None
                )
            if grant is None or (
                plate.grant_kind == "READY"
                and not await self._ready_to_pick(session, work, plate, grant)
            ):
                return plate
            response = await read_decision(self.requests, session, grant)
            expected = (
                (
                    response.response_data.get("source")
                    if plate.grant_kind == "READY"
                    else response.response_data.get("next_source_action")
                )
                if response
                else None
            )
            if plate.grant_kind == "RETRY":
                parent = await session.get(AutoInboundPlate, grant.plate_id)
                if (
                    parent is None
                    or parent.bin_work_id != work.id
                    or parent.final_decision_round_id != grant.id
                    or grant.operation != "putaway.source.empty_decide@v1"
                    or response is None
                    or response.response_data != {"result": "RETRY"}
                    or not await self._empty_result(session, parent)
                    or parent.source_locator != plate.source_locator
                    or parent.source_params != plate.source_params
                ):
                    return plate
                expected = {**parent.source_locator, **parent.source_params}
            if expected != {**plate.source_locator, **plate.source_params}:
                return plate
            role = next((b for b in bindings.values() if b and b.device_role == "RACK_ARM"), None)
            station = dict(settings.source_locations).get(proof["location_code"])
            if role is None or station is None:
                return plate
            source = InboundSource.model_validate(expected).model_dump()
            params = dict(
                source=dict(
                    location_id=station,
                    location_type="ONE_LAYER_RACK",
                    rack_id=source["rack_id"],
                    bin_id=source["bin_code"],
                    cell_id=source["cell_id"],
                    bin_location=source["bin_location"],
                    bin_type=str(source["bin_type"]),
                    bin_cell_location=source["cell_index"],
                    **{
                        k: source[k]
                        for k in (
                            "reel_layer",
                            "reel_thickness",
                            "reel_diameter",
                            "reel_totalthickness",
                        )
                    },
                ),
                target=SCAN,
            )
            command = PickAndPut.from_millimeters(**params)
            params = command.model_dump()
            view = await self.ecs.create(
                session,
                owner=OWNER,
                call_key=plate.pick_call_key,
                device_code=role.device_code,
                timeout_ms=settings.rack_arm_timeout_ms,
                command=command,
            )
            plate.pick_command_code = view.command_code
            plate.source_arrival_snapshot = {
                **plate.source_arrival_snapshot,
                "pick_command": dict(
                    device_code=role.device_code,
                    timeout_ms=settings.rack_arm_timeout_ms,
                    params=params,
                ),
            }
        await observe_arm_result(self.ecs, session, plate, "pick", owner=OWNER)
        empty = await self._empty_result(session, plate)
        if empty is not None:
            await self._empty(session, plate, work, task, empty, now_ms, allow_continue=enabled)
            return plate
        pick = await self._result(session, plate, "pick")
        if pick is None:
            return plate
        if plate.pick_consumed_at_ms is None:
            plate.pick_result_snapshot = pick.model_dump()
            plate.pick_result_ref = canonical_digest(pick.model_dump())
            plate.pick_consumed_at_ms = now_ms
            plate.barcode = pick.data["barcode"]
            plate.scanned_at_ms = pick.data["scanned_at"]
        context = dict(
            task_id=task.task_id,
            target_bin_code=work.bin_code,
            source={k: plate.source_locator[k] for k in ("rack_id", "bin_code", "cell_id")},
            barcode=pick.data["barcode"],
            scanned_at=pick.data["scanned_at"],
        )
        stage = f"plate:{plate.id}:material"
        round_ = await session.scalar(
            select(AutoInboundDecisionRound)
            .where(
                AutoInboundDecisionRound.plate_id == plate.id,
                AutoInboundDecisionRound.purpose == "MATERIAL_DECIDE",
            )
            .order_by(AutoInboundDecisionRound.round_no.desc())
            .limit(1)
        )
        if plate.final_decision_round_id is not None and (
            round_ is None or round_.id != plate.final_decision_round_id
        ):
            return plate
        retry = False
        if round_ is not None:
            if (
                round_.task_id != task.task_id
                or round_.stage_key != stage
                or round_.operation != DECIDE
                or not isinstance(round_.request_data, dict)
                or round_.request_data
                != {**context, "source_racks": round_.request_data.get("source_racks")}
            ):
                return plate
            due = await self._material_continuation_due(session, plate, round_)
            if due is None or now_ms < due:
                return plate
            response = await read_decision(self.requests, session, round_)
            if response is None:
                return plate
            data = response.response_data
            snapshot = dict(
                http_status=200, code="DECIDED", data=data, timestamp=response.response_timestamp
            )
            if not round_.unresolved and round_.response_snapshot != snapshot:
                return plate
            if isinstance(data, dict) and data.get("result") == "WAIT":
                try:
                    wait = Wait.model_validate(data)
                except ValidationError:
                    return plate
                if plate.final_decision_round_id is not None:
                    return plate
                if round_.unresolved:
                    round_.response_snapshot = snapshot
                    round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
                    round_.wait_recorded_at_ms = now_ms
                    round_.retry_after_ms = wait.retry_after_ms
                    round_.next_eligible_at_ms = now_ms + wait.retry_after_ms
                    round_.unresolved = False
                    await self.work_plans._invalidate(session, work_line_id)
                    return plate
                if (
                    round_.wait_recorded_at_ms is None
                    or round_.wait_recorded_at_ms != round_.response_recorded_at_ms
                    or round_.wait_recorded_at_ms != round_.consumed_at_ms
                    or round_.retry_after_ms != wait.retry_after_ms
                    or round_.next_eligible_at_ms
                    != round_.wait_recorded_at_ms + wait.retry_after_ms
                    or now_ms < round_.next_eligible_at_ms
                    or task.status not in ("EXECUTING", "CANCELLED")
                    or work.terminal_basis is not None
                    or not await arrival_is_open(self.roller, session, work)
                ):
                    return plate
                retry = True
        if round_ is None or retry:
            sources, proofs = await self.work_plans._inbound_sources(session, task, bindings)
            if sources is None:
                return plate
            data = {**context, "source_racks": sources}
            operation_id = str(uuid7())
            ref = await self.requests.send(
                session, MaterialDecide(**data), operation_id=operation_id
            )
            session.add(
                AutoInboundDecisionRound(
                    task_id=task.task_id,
                    plate_id=plate.id,
                    purpose="MATERIAL_DECIDE",
                    stage_key=stage,
                    round_no=round_.round_no + 1 if round_ else 1,
                    previous_round_id=round_.id if round_ else None,
                    operation=DECIDE,
                    operation_id=operation_id,
                    message_id=ref.message_id,
                    request_data=data,
                    source_rack_bindings=proofs,
                    unresolved=True,
                )
            )
            await self.work_plans._invalidate(session, work_line_id)
            await session.flush()
            return plate
        if isinstance(data, dict) and data.get("result") == "REJECT":
            try:
                Reject.model_validate(data)
            except ValidationError:
                return plate
            await self._ng(session, plate, work, round_, snapshot, bindings, enabled, now_ms)
            return plate
        if (
            not isinstance(data, dict)
            or data.get("result") != "ACCEPT"
            or (not round_.unresolved and plate.final_decision_round_id != round_.id)
            or set(data) - {"result", "target", "source_rack_exhausted", "next_source_action"}
            or ("source_rack_exhausted" in data and data["source_rack_exhausted"] is not True)
        ):
            return plate
        try:
            target = Target.model_validate(data.get("target")).model_dump()
        except (ValidationError, TypeError):
            return plate
        if target["bin_code"] != work.bin_code:
            return plate
        # Current PUT is independent of whether the proposed successor is valid.
        if round_.unresolved:
            round_.response_snapshot = snapshot
            round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
            round_.unresolved = False
            plate.final_decision_round_id = round_.id
            plate.target_snapshot = {"target": target}
            plate.next_source_action = data.get("next_source_action")
            plate.put_call_key = f"auto:{task.task_id}:plate:{plate.id}:put"
        if "next_source_action" in data:
            await self._next(session, plate, round_, work, data)
        if plate.put_command_code is None:
            role = next((b for b in bindings.values() if b and b.device_role == "BIN_ARM"), None)
            if not enabled or role is None or not await arrival_is_open(self.roller, session, work):
                return plate
            params = dict(
                source=SCAN,
                target=dict(
                    location_id="STATION_WORK1",
                    location_type="BIN",
                    bin_type=str(target["bin_type"]),
                    bin_cell_location=target["cell_index"],
                    **{
                        k: target[k]
                        for k in (
                            "reel_layer",
                            "reel_thickness",
                            "reel_diameter",
                            "reel_totalthickness",
                        )
                    },
                ),
            )
            command = PickAndPut.from_millimeters(**params)
            params = command.model_dump()
            view = await self.ecs.create(
                session,
                owner=OWNER,
                call_key=plate.put_call_key,
                device_code=role.device_code,
                timeout_ms=settings.bin_arm_timeout_ms,
                command=command,
            )
            plate.put_command_code = view.command_code
            plate.target_snapshot = {
                "target": target,
                "put_command": dict(
                    device_code=role.device_code,
                    timeout_ms=settings.bin_arm_timeout_ms,
                    params=params,
                ),
            }
        await observe_arm_result(self.ecs, session, plate, "put", owner=OWNER)
        put = await self._result(session, plate, "put")
        if put is None:
            return plate
        if plate.put_consumed_at_ms is None:
            plate.put_result_snapshot = put.model_dump()
            plate.put_result_ref = canonical_digest(put.model_dump())
            plate.put_consumed_at_ms = now_ms
        if plate.report_message_id is None:
            report = dict(
                task_id=task.task_id,
                source=round_.request_data["source"],
                barcode=pick.data["barcode"],
                target={k: target[k] for k in ("bin_code", "cell_id")},
                occurred_at=put.finish_time,
            )
            plate.report_operation_id = str(uuid7())
            ref = await self.requests.send(
                session, MovementReport(**report), operation_id=plate.report_operation_id
            )
            plate.report_message_id, plate.report_data = ref.message_id, report
        await session.flush()
        return plate

    async def advance_report(self, session, *, plate_id, now_ms):
        """Consume only the registered original report in the caller's RR transaction."""
        plate = await session.get(
            AutoInboundPlate, plate_id, with_for_update=True, populate_existing=True
        )
        if plate is None or plate.report_consumed_at_ms is not None:
            return plate
        report = await self._normal_report(session, plate)
        if report is None:
            return plate
        plate.report_consumed_at_ms = now_ms
        plate.pending = False
        plate.physical_closure_basis = dict(
            kind="NORMAL_REPORTED",
            command_code=plate.put_command_code,
            operation_id=plate.report_operation_id,
        )
        plate.report_response = dict(
            http_status=report.http_status,
            code=report.response_code,
            timestamp=report.response_timestamp,
            data=report.response_data,
        )
        await session.flush()
        return plate

    async def _empty_result(self, session, plate):
        command = (plate.source_arrival_snapshot or {}).get("pick_command", {})
        view = await self.ecs.get_by_call(session, owner=OWNER, call_key=plate.pick_call_key)
        if (
            view is None
            or not plate.pick_command_code
            or view.command_code != plate.pick_command_code
            or view.device_code != command.get("device_code")
            or view.task_type != "PICK_AND_PUT"
            or view.status != "FAILED"
            or view.hold_reason
            or view.needs_reconciliation
            or view.result is None
        ):
            return None
        try:
            result = ResultEnvelope.model_validate(view.result)
            data = ArmResult.model_validate(result.data)
        except (ValueError, TypeError):
            return None
        if (
            result.command_code != plate.pick_command_code
            or result.device_code != view.device_code
            or result.result != "FAILED"
            or data.actual_qty != 0
            or data.pick_and_put_result != "PICK_EMPTY"
            or result.error_detail is None
            or data.barcode is not None
        ):
            return None
        return result

    async def _empty(self, session, plate, work, task, result, now_ms, *, allow_continue):
        context = dict(
            task_id=task.task_id,
            source={k: plate.source_locator[k] for k in ("rack_id", "bin_code", "cell_id")},
            observed_at=result.finish_time,
        )
        plate.empty_observation = context
        plate.pick_consumed_at_ms = plate.pick_consumed_at_ms or now_ms
        round_ = await session.scalar(
            select(AutoInboundDecisionRound)
            .where(
                AutoInboundDecisionRound.plate_id == plate.id,
                AutoInboundDecisionRound.purpose == "EMPTY_DECIDE",
            )
            .order_by(AutoInboundDecisionRound.round_no.desc())
            .limit(1)
        )
        if round_ is not None:
            if round_.request_data != context:
                return
            due = await self._material_continuation_due(session, plate, round_)
            if due is None or now_ms < due:
                return
            response = await read_decision(self.requests, session, round_)
            if response is None:
                return
            data = response.response_data
            try:
                decision = {"WAIT": Wait, "RETRY": Retry, "SOURCE_ABSENT": SourceAbsent}[
                    data["result"]
                ].model_validate(data)
            except (ValueError, TypeError, KeyError):
                return
            snapshot = dict(
                http_status=200, code="DECIDED", data=data, timestamp=response.response_timestamp
            )
            if not round_.unresolved and round_.response_snapshot != snapshot:
                return
            if round_.unresolved:
                round_.response_snapshot = snapshot
                round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
                round_.unresolved = False
                if decision.result == "WAIT":
                    round_.wait_recorded_at_ms = now_ms
                    round_.retry_after_ms = decision.retry_after_ms
                    round_.next_eligible_at_ms = now_ms + decision.retry_after_ms
                    await session.flush()
                    return
                plate.final_decision_round_id = round_.id
            if decision.result == "WAIT":
                if (
                    round_.wait_recorded_at_ms != round_.consumed_at_ms
                    or round_.retry_after_ms != decision.retry_after_ms
                    or round_.next_eligible_at_ms != round_.consumed_at_ms + decision.retry_after_ms
                    or now_ms < round_.next_eligible_at_ms
                ):
                    return
            else:
                if plate.final_decision_round_id != round_.id:
                    return
                plate.physical_closure_basis = dict(
                    kind="EMPTY", command_code=plate.pick_command_code, round_id=round_.id
                )
                if decision.result == "RETRY":
                    plate.pending = False
                    existing = await session.scalar(
                        select(AutoInboundPlate).where(AutoInboundPlate.grant_round_id == round_.id)
                    )
                    if existing is None:
                        session.add(
                            AutoInboundPlate(
                                task_id=plate.task_id,
                                bin_work_id=work.id,
                                source_rack_job_id=plate.source_rack_job_id,
                                source_preparation_action_id=plate.source_preparation_action_id,
                                grant_kind="RETRY",
                                grant_round_id=round_.id,
                                grant_snapshot=data,
                                source_locator=plate.source_locator,
                                source_params=plate.source_params,
                                source_arrival_snapshot={
                                    k: v
                                    for k, v in plate.source_arrival_snapshot.items()
                                    if k != "pick_command"
                                },
                                pick_call_key=f"auto:{task.task_id}:retry:{round_.id}",
                                pending=True,
                            )
                        )
                else:
                    plate.source_closed_basis = dict(kind="SOURCE_ABSENT", round_id=round_.id)
                    if allow_continue and await self._continue(session, plate, work, now_ms):
                        plate.pending = False
                await session.flush()
                return
        operation_id = str(uuid7())
        ref = await self.requests.send(session, EmptyDecide(**context), operation_id=operation_id)
        session.add(
            AutoInboundDecisionRound(
                task_id=task.task_id,
                plate_id=plate.id,
                purpose="EMPTY_DECIDE",
                stage_key=f"plate:{plate.id}:empty",
                round_no=round_.round_no + 1 if round_ else 1,
                previous_round_id=round_.id if round_ else None,
                operation="putaway.source.empty_decide@v1",
                operation_id=operation_id,
                message_id=ref.message_id,
                request_data=context,
                unresolved=True,
            )
        )
        await self.work_plans._invalidate(session, work.work_line_id)
        await session.flush()

    async def _ng(self, session, plate, work, round_, snapshot, bindings, enabled, now_ms):
        if round_.unresolved:
            round_.response_snapshot = snapshot
            round_.response_recorded_at_ms = round_.consumed_at_ms = now_ms
            round_.unresolved = False
            plate.final_decision_round_id = round_.id
            plate.target_snapshot = {"ng": snapshot["data"]["ng_locator"]}
            plate.put_call_key = f"auto:{plate.task_id}:plate:{plate.id}:ng"
        if plate.final_decision_round_id != round_.id:
            return
        if plate.put_command_code is None:
            role = next((b for b in bindings.values() if b and b.device_role == "BIN_ARM"), None)
            if not enabled or role is None or not await arrival_is_open(self.roller, session, work):
                return
            params = dict(
                source=SCAN, target=dict(location_id="STATION_NG", location_type="SCAN_NG")
            )
            command = PickAndPut.from_millimeters(**params)
            params = command.model_dump()
            view = await self.ecs.create(
                session,
                owner=OWNER,
                call_key=plate.put_call_key,
                device_code=role.device_code,
                timeout_ms=self.settings[work.work_line_id].bin_arm_timeout_ms,
                command=command,
            )
            plate.put_command_code = view.command_code
            plate.target_snapshot = {
                **plate.target_snapshot,
                "put_command": dict(
                    device_code=role.device_code,
                    timeout_ms=self.settings[work.work_line_id].bin_arm_timeout_ms,
                    params=params,
                ),
            }
        await observe_arm_result(self.ecs, session, plate, "put", owner=OWNER)
        result = await self._result(session, plate, "put")
        if result is None:
            return
        plate.put_consumed_at_ms = plate.put_consumed_at_ms or now_ms
        plate.physical_closure_basis = dict(
            kind="NG", command_code=plate.put_command_code, round_id=round_.id
        )
        if snapshot["data"]["source_disposition"] == "CLOSE":
            plate.source_closed_basis = dict(kind="CLOSE", round_id=round_.id)
        if enabled and await self._continue(session, plate, work, now_ms):
            plate.pending = False
        await session.flush()

    async def _continue(self, session, plate, work, now_ms) -> bool:
        if not await self.closed(session, plate) or not await arrival_is_open(
            self.roller, session, work
        ):
            return False
        # Independent normal reports do not prevent a new source evaluation.
        for other in await session.scalars(
            select(AutoInboundPlate).where(
                AutoInboundPlate.bin_work_id == work.id, AutoInboundPlate.id != plate.id
            )
        ):
            if other.final_decision_round_id is None:
                return False
            final = await session.get(AutoInboundDecisionRound, other.final_decision_round_id)
            if final is None or final.unresolved:
                return False
            data = (final.response_snapshot or {}).get("data", {})
            if "next_source_action" in data:
                child = await session.scalar(
                    select(AutoInboundPlate).where(
                        AutoInboundPlate.grant_parent_plate_id == other.id
                    )
                )
                source = data["next_source_action"]
                if (
                    child is None
                    or child.task_id != plate.task_id
                    or child.bin_work_id != work.id
                    or child.grant_kind != "NEXT"
                    or child.grant_snapshot != data
                    or not isinstance(source, dict)
                    or {**child.source_locator, **child.source_params} != source
                    or child.source_arrival_snapshot.get("source")
                    != (final.source_rack_bindings or {}).get(source.get("rack_id"))
                    or child.pick_consumed_at_ms is None
                    or not (
                        await self._result(session, child, "pick")
                        or await self._empty_result(session, child)
                    )
                ):
                    return False
        continuation = await self.work_plans.start_bin(
            session,
            work_line_id=work.work_line_id,
            arrival_id=work.arrival_id,
            now_ms=now_ms,
            trigger_plate_id=plate.id,
        )
        return continuation is not None and continuation.trigger_plate_id == plate.id

    async def _next(self, session, plate, round_, work, data):
        try:
            source = InboundSource.model_validate(data["next_source_action"]).model_dump()
        except (ValidationError, TypeError):
            return
        proof = (round_.source_rack_bindings or {}).get(source["rack_id"])
        if (
            not proof
            or {k: source[k] for k in ("rack_id", "location_code")}
            not in round_.request_data["source_racks"]
            or (
                data.get("source_rack_exhausted") is True
                and source["rack_id"] == plate.source_locator["rack_id"]
            )
        ):
            return
        if await session.scalar(
            select(AutoInboundPlate.id).where(AutoInboundPlate.grant_parent_plate_id == plate.id)
        ):
            return
        locator = {
            k: source[k]
            for k in ("rack_id", "location_code", "bin_code", "bin_location", "cell_id")
        }
        session.add(
            AutoInboundPlate(
                task_id=plate.task_id,
                bin_work_id=work.id,
                source_rack_job_id=proof["rack_job_id"],
                source_preparation_action_id=proof["preparation_action_id"],
                grant_kind="NEXT",
                grant_parent_plate_id=plate.id,
                grant_snapshot=data,
                source_locator=locator,
                source_params={k: v for k, v in source.items() if k not in locator},
                source_arrival_snapshot=dict(
                    arrival_id=work.arrival_id,
                    work_line_id=work.work_line_id,
                    scanned_at=work.scanned_at,
                    source=proof,
                ),
                pick_call_key=f"auto:{plate.task_id}:next:{plate.id}",
                pending=True,
            )
        )
        await session.flush()

    async def _material_continuation_due(self, session, plate, current):
        rounds = (
            await session.scalars(
                select(AutoInboundDecisionRound)
                .where(
                    AutoInboundDecisionRound.plate_id == plate.id,
                    AutoInboundDecisionRound.purpose == current.purpose,
                )
                .order_by(AutoInboundDecisionRound.round_no)
            )
        ).all()
        if not rounds or rounds[-1].id != current.id or not isinstance(current.request_data, dict):
            return None
        previous_id, due = None, 0
        stage_name = "empty" if current.purpose == "EMPTY_DECIDE" else "material"
        for index, round_ in enumerate(rounds, start=1):
            if (
                round_.task_id != plate.task_id
                or round_.stage_key != f"plate:{plate.id}:{stage_name}"
                or round_.operation
                != (
                    "putaway.source.empty_decide@v1"
                    if current.purpose == "EMPTY_DECIDE"
                    else DECIDE
                )
                or round_.round_no != index
                or round_.previous_round_id != previous_id
                or round_.unresolved != (round_.consumed_at_ms is None)
                or (round_.consumed_at_ms is not None and round_.consumed_at_ms < due)
                or not isinstance(round_.request_data, dict)
                or round_.request_data
                != (
                    current.request_data
                    if current.purpose == "EMPTY_DECIDE"
                    else {
                        **current.request_data,
                        "source_racks": round_.request_data.get("source_racks"),
                    }
                )
            ):
                return None
            if round_.id == current.id:
                return due
            if round_.unresolved:
                return None
            response = await read_decision(self.requests, session, round_)
            if response is None or round_.response_snapshot != {
                "http_status": 200,
                "code": "DECIDED",
                "data": response.response_data,
                "timestamp": response.response_timestamp,
            }:
                return None
            try:
                wait = Wait.model_validate(response.response_data)
            except ValidationError:
                return None
            due = round_.consumed_at_ms + wait.retry_after_ms
            if (
                round_.wait_recorded_at_ms != round_.consumed_at_ms
                or round_.response_recorded_at_ms != round_.consumed_at_ms
                or round_.retry_after_ms != wait.retry_after_ms
                or round_.next_eligible_at_ms != due
            ):
                return None
            previous_id = round_.id
        return None

    async def closed(self, session, plate):
        round_ = (
            await session.get(AutoInboundDecisionRound, plate.final_decision_round_id)
            if plate.final_decision_round_id
            else None
        )
        if (
            round_ is not None
            and not round_.unresolved
            and await self._material_continuation_due(session, plate, round_) is not None
        ):
            response = await read_decision(self.requests, session, round_)
            data = response.response_data if response else None
            if data == (round_.response_snapshot or {}).get("data") and isinstance(data, dict):
                try:
                    if data.get("result") in ("SOURCE_ABSENT", "RETRY"):
                        (
                            SourceAbsent if data["result"] == "SOURCE_ABSENT" else Retry
                        ).model_validate(data)
                        empty = await self._empty_result(session, plate)
                        return bool(
                            empty
                            and round_.request_data
                            == dict(
                                task_id=plate.task_id,
                                source={
                                    k: plate.source_locator[k]
                                    for k in ("rack_id", "bin_code", "cell_id")
                                },
                                observed_at=empty.finish_time,
                            )
                        )
                    if data.get("result") == "REJECT":
                        Reject.model_validate(data)
                        return bool(
                            await self._result(session, plate, "pick")
                            and await self._result(session, plate, "put")
                        )
                except (ValueError, TypeError):
                    return False
        return bool(
            plate.report_consumed_at_ms is not None and await self._normal_report(session, plate)
        )

    async def _normal_report(self, session, plate):
        pick, put = (
            await self._result(session, plate, "pick"),
            await self._result(session, plate, "put"),
        )
        if pick is None or put is None or plate.report_message_id is None:
            return None
        round_ = await session.get(AutoInboundDecisionRound, plate.final_decision_round_id)
        if (
            round_ is None
            or round_.unresolved
            or await self._material_continuation_due(session, plate, round_) is None
        ):
            return None
        response = await read_decision(self.requests, session, round_)
        if response is None or response.response_data != (round_.response_snapshot or {}).get(
            "data"
        ):
            return None
        target = response.response_data.get("target", {})
        expected = dict(
            task_id=plate.task_id,
            source={k: plate.source_locator[k] for k in ("rack_id", "bin_code", "cell_id")},
            barcode=pick.data["barcode"],
            target={k: target.get(k) for k in ("bin_code", "cell_id")},
            occurred_at=put.finish_time,
        )
        if plate.report_data != expected:
            return None
        report = await self.requests.get_result(
            session,
            message_id=plate.report_message_id,
            operation=REPORT,
            operation_id=plate.report_operation_id,
        )
        valid = (
            (report.message_id, report.operation, report.operation_id)
            == (plate.report_message_id, REPORT, plate.report_operation_id)
            and report.availability == "FOUND"
            and report.response_state == "VALID"
            and report.http_status == 200
            and report.response_code in ("RECORDED", "DUPLICATE")
            and report.response_data == {}
        )
        return report if valid else None

    async def source_clear(self, session, *, job_id):
        """Only original source dependencies gate handoff; a picked plate's report does not."""
        plates = list(
            await session.scalars(
                select(AutoInboundPlate).where(AutoInboundPlate.source_rack_job_id == job_id)
            )
        )
        for plate in plates:
            empty = await self._empty_result(session, plate)
            if empty is not None:
                final = (
                    await session.get(AutoInboundDecisionRound, plate.final_decision_round_id)
                    if plate.final_decision_round_id
                    else None
                )
                if final is None or not await self.closed(session, plate):
                    return False
                if final.response_snapshot["data"]["result"] == "RETRY":
                    retry = await session.scalar(
                        select(AutoInboundPlate).where(AutoInboundPlate.grant_round_id == final.id)
                    )
                    if retry is None:
                        return False
            elif await self._result(session, plate, "pick") is None:
                return False
        rounds = list(await session.scalars(select(AutoInboundDecisionRound)))
        for round_ in rounds:
            if round_.unresolved and any(
                proof.get("rack_job_id") == job_id
                for proof in (round_.source_rack_bindings or {}).values()
            ):
                return False
            if round_.plate_id in {p.id for p in plates} and not round_.unresolved:
                data = (round_.response_snapshot or {}).get("data", {})
                if "next_source_action" in data:
                    source = data["next_source_action"]
                    if isinstance(source, dict) and source.get("rack_id") in (
                        round_.source_rack_bindings or {}
                    ):
                        proof = round_.source_rack_bindings[source["rack_id"]]
                        if proof.get("rack_job_id") == job_id and not await session.scalar(
                            select(AutoInboundPlate.id).where(
                                AutoInboundPlate.grant_parent_plate_id == round_.plate_id
                            )
                        ):
                            return False
        return True

    async def bin_closed(self, session, work):
        plates = tuple(
            await session.scalars(
                select(AutoInboundPlate).where(AutoInboundPlate.bin_work_id == work.id)
            )
        )
        if not plates or any(plate.task_id != work.task_id for plate in plates):
            return False
        endings = []
        for plate in plates:
            if not await self.closed(session, plate):
                return False
            round_ = await session.get(AutoInboundDecisionRound, plate.final_decision_round_id)
            data = (round_.response_snapshot or {}).get("data", {})
            if data.get("result") == "ACCEPT":
                if "next_source_action" not in data:
                    endings.append(plate.id)
                elif not any(p.grant_parent_plate_id == plate.id for p in plates):
                    return False
            elif data.get("result") == "RETRY":
                if not any(
                    p.grant_kind == "RETRY" and p.grant_round_id == round_.id for p in plates
                ):
                    return False
            else:
                continuation = await session.scalar(
                    select(AutoInboundDecisionRound).where(
                        AutoInboundDecisionRound.bin_work_id == work.id,
                        AutoInboundDecisionRound.trigger_plate_id == plate.id,
                    )
                )
                if continuation is None:
                    return False
        rounds = list(
            await session.scalars(
                select(AutoInboundDecisionRound)
                .where(AutoInboundDecisionRound.bin_work_id == work.id)
                .order_by(AutoInboundDecisionRound.round_no)
            )
        )
        previous = None
        for index, round_ in enumerate(rounds, start=1):
            if (
                round_.unresolved
                or round_.round_no != index
                or round_.previous_round_id != (previous.id if previous else None)
                or round_.task_id != work.task_id
                or round_.operation != "putaway.work_plan@v1"
                or round_.request_data
                != dict(
                    task_id=work.task_id,
                    target_bin_code=work.bin_code,
                    scanned_at=work.scanned_at,
                    source_racks=round_.request_data.get("source_racks"),
                )
            ):
                return False
            response = await read_decision(self.requests, session, round_)
            if response is None or response.response_data != (round_.response_snapshot or {}).get(
                "data"
            ):
                return False
            data = response.response_data
            try:
                DECISIONS["inbound"].validate_python(data)
            except (ValueError, TypeError):
                return False
            if round_.consumed_at_ms is None:
                return False
            if previous:
                prior = previous.response_snapshot["data"]
                if prior.get("result") == "WAIT":
                    if (
                        previous.next_eligible_at_ms is None
                        or previous.wait_recorded_at_ms != previous.consumed_at_ms
                        or previous.retry_after_ms != prior["retry_after_ms"]
                        or previous.next_eligible_at_ms
                        != previous.consumed_at_ms + prior["retry_after_ms"]
                        or round_.consumed_at_ms < previous.next_eligible_at_ms
                    ):
                        return False
                elif round_.trigger_plate_id is None:
                    return False
                else:
                    trigger = next((p for p in plates if p.id == round_.trigger_plate_id), None)
                    final = (
                        await session.get(AutoInboundDecisionRound, trigger.final_decision_round_id)
                        if trigger
                        else None
                    )
                    if final is None or (final.response_snapshot or {}).get("data", {}).get(
                        "result"
                    ) not in ("REJECT", "SOURCE_ABSENT"):
                        return False
            if data.get("result") == "READY" and not any(
                p.grant_kind == "READY" and p.grant_round_id == round_.id for p in plates
            ):
                return False
            previous = round_
        if rounds and rounds[-1].response_snapshot["data"] == {"result": "NO_WORK"}:
            return work.terminal_basis == dict(
                kind="NO_WORK", round_id=rounds[-1].id, operation_id=rounds[-1].operation_id
            )
        return len(endings) == 1
