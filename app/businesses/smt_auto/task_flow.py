"""AUTO-owned original release and completion recovery; caller owns one transaction."""

from sqlalchemy import select

from app.businesses._components.bin_roller.completion import release_intent, supply_accounted_for
from app.businesses._components.bin_roller.ports import Scan2Arrival, SourceFaceRef
from app.businesses._components.sorting_task.contracts import CompletionDecision, PrepareAccepted
from app.businesses._components.sorting_task.request.sorting_task_completion_confirm_v1 import (
    CompletionConfirm,
)
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.integrations.ecs_command.contracts import ResultEnvelope
from app.reliable import ScopeRef

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
from .roller_ports import RollerPorts, matching_arrival
from .work_plan_contracts import DECISIONS


async def withdrawn_outbound_source(session, *, dispatch, task, member, inputs, plates):
    """Original selector plus never-started production and original rack handoff proof."""
    from .rack_ports import AutoRackPorts

    if (
        not AutoRackPorts._withdrawn(member, inputs)
        or member.first_work_plan_round_id is not None
        or any(p.source_member_id == member.id for p in plates)
    ):
        return False
    if member.rack_job_id is None:
        return member.preparation_action_id is None
    if dispatch is None or member.preparation_action_id is None:
        return False
    progress = await dispatch.progress(session, job_id=member.rack_job_id)
    if progress is None or (
        progress.origin.source_kind,
        progress.origin.task_id,
        progress.origin.work_line_id,
        progress.origin.rack_id,
    ) != ("AUTO_OUTBOUND_RETURN", task.task_id, task.work_line_id, member.rack_id):
        return False
    action = next((a for a in progress.actions if a.id == member.preparation_action_id), None)
    return bool(
        action
        and action.job_id == progress.id
        and action.kind in ("ENTER", "ROTATE")
        and action.source_ref == f"OUTBOUND_MEMBER:{member.id}"
        and action.target_face == member.rack_face
        and (
            progress.state in ("WITHDRAWN", "NOT_ARRIVED")
            or (
                action.phase == "APPLIED"
                and action.outcome
                and action.outcome.get("status") == "SUCCEEDED"
                and action.actual_face == action.target_face
                and action.actual_position
                == action.target
                == {"kind": "RACK_POSITION", "location_code": progress.origin.point}
            )
        )
    )


def supply_member_closed(member, facts, inputs):
    """Completed feed or exactly cancelled original never-started public supply."""
    if len(facts) != 1 or not facts[0].original_ids or facts[0].pending:
        return False
    if facts[0].feed_complete:
        return True
    return bool(
        facts[0].withdrawn_before_start
        and member.withdrawn_at_ms is not None
        and any(
            row.id == member.withdrawn_by_input_id
            and row.task_id == member.task_id
            and row.disposition == "APPLIED"
            and row.operation == "sorting.task.cancel@v1"
            and row.effective_request.get("cancel_scope") == "PLAN_MEMBERS"
            and any(
                rack["plan_revision"] == member.plan_revision
                and rack["rack_id"] == member.rack_id
                and member.rack_face in rack["rack_face"]
                for rack in row.effective_request.get("bin_source_racks", ())
            )
            for row in inputs
        )
    )


class TaskFlow:
    def __init__(
        self, requests, *, roller=None, dispatch=None, work_plans=None, inbound=None, outbound=None
    ):
        self.requests, self.roller = requests, roller
        self.dispatch, self.work_plans = dispatch, work_plans
        self.inbound, self.outbound = inbound, outbound

    async def original_inprocess(self, session, *, work_line_id, bin_code, arrival_id=None):
        """Read all original responsibilities for this BIN on this line, never latest-task."""
        rows = tuple(
            await session.scalars(
                select(AutoBinWork)
                .where(AutoBinWork.work_line_id == work_line_id, AutoBinWork.bin_code == bin_code)
                .order_by(AutoBinWork.id)
            )
        )
        result = []
        for row in rows:
            if arrival_id is not None and row.arrival_id != arrival_id:
                continue
            # A saved timestamp/JSON alone cannot erase accepted production responsibility.
            if not await self._bin_terminal(session, row) or not await self._released(session, row):
                result.append(row)
                continue
        return tuple(result)

    async def _released(self, session, work):
        if (
            self.roller is None
            or not work.release_command_code
            or work.release_consumed_at_ms is None
        ):
            return False
        view = await self.roller.get_scan2_release_result(session, arrival_id=work.arrival_id)
        result = self._release_success(work, view)
        if result is None:
            return False
        data = result.model_dump()
        return (
            work.release_result_ref == canonical_digest(data)
            and work.release_result_snapshot == data
        )

    @staticmethod
    def _release_success(work, view):
        if (
            view is None
            or view.command_code != work.release_command_code
            or view.task_type != "MOVE_FORWARD"
            or view.device_code != work.device_code
            or view.status != "SUCCEEDED"
            or view.hold_reason
            or view.needs_reconciliation
            or view.result is None
        ):
            return None
        try:
            result = ResultEnvelope.model_validate(view.result)
        except (ValueError, TypeError):
            return None
        if (
            result.command_code != work.release_command_code
            or result.device_code != work.device_code
            or result.result != "SUCCESS"
        ):
            return None
        return result

    async def _bin_terminal(self, session, work):
        if work.bin_code is None or work.production_hold_reason is not None:
            return False
        if work.association == "NO_TASK":
            if (
                self.roller is None
                or work.task_id is not None
                or work.business_type is not None
                or work.exception_basis is not None
                or work.raw_bin_code != f"{work.bin_code}-C"
            ):
                return False
            return await matching_arrival(self.roller, session, work)
        if work.association != "TASK" or work.task_id is None:
            return False
        task = await session.get(AutoTask, work.task_id)
        if (
            task is None
            or task.work_line_id != work.work_line_id
            or task.business_type != work.business_type
        ):
            return False
        has_plates = False
        for model, direction, reader in (
            (AutoInboundPlate, "inbound", self.inbound),
            (AutoOutboundPlate, "outbound", self.outbound),
        ):
            plate_id = await session.scalar(
                select(model.id).where(model.bin_work_id == work.id).limit(1)
            )
            if plate_id is None:
                continue
            if work.business_type != direction or reader is None:
                return False
            if not await reader.bin_closed(session, work):
                return False
            has_plates = True
        if has_plates:
            return True
        models = (AutoInboundDecisionRound, AutoOutboundDecisionRound)
        rounds = []
        for model in models:
            rounds.extend(await session.scalars(select(model).where(model.bin_work_id == work.id)))
        if not rounds or any(r.task_id != work.task_id or r.unresolved for r in rounds):
            return False
        rounds.sort(key=lambda r: r.round_no)
        last = rounds[-1]
        if work.terminal_basis != {
            "kind": "NO_WORK",
            "round_id": last.id,
            "operation_id": last.operation_id,
        }:
            return False
        inbound = work.business_type == "inbound"
        expected_model = AutoInboundDecisionRound if inbound else AutoOutboundDecisionRound
        expected_operation = "putaway.work_plan@v1" if inbound else "outbound.bin.work_plan@v1"
        previous = None
        for index, round_ in enumerate(rounds, start=1):
            expected_data = (
                {
                    "task_id": work.task_id,
                    "target_bin_code": work.bin_code,
                    "scanned_at": work.scanned_at,
                    "source_racks": round_.request_data.get("source_racks"),
                }
                if inbound
                else {
                    "task_id": work.task_id,
                    "bin_code": work.bin_code,
                    "scanned_at": work.scanned_at,
                }
            )
            if (
                not isinstance(round_, expected_model)
                or round_.purpose != "WORK_PLAN"
                or round_.stage_key != f"bin:{work.id}:first"
                or round_.round_no != index
                or round_.previous_round_id != (previous.id if previous else None)
                or round_.operation != expected_operation
                or round_.consumed_at_ms is None
                or round_.request_data != expected_data
                or (previous is not None and round_.consumed_at_ms < previous.next_eligible_at_ms)
            ):
                return False
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
                or result.http_status != 200
                or result.response_code != "DECIDED"
                or (round_.response_snapshot or {}).get("data") != result.response_data
            ):
                return False
            try:
                decision = DECISIONS["inbound" if inbound else "bin"].validate_python(
                    result.response_data
                )
            except (ValueError, TypeError):
                return False
            if round_ is last:
                if decision.result != "NO_WORK":
                    return False
            elif (
                decision.result != "WAIT"
                or round_.wait_recorded_at_ms != round_.consumed_at_ms
                or round_.retry_after_ms != decision.retry_after_ms
                or round_.next_eligible_at_ms
                != round_.wait_recorded_at_ms + decision.retry_after_ms
            ):
                return False
            previous = round_
        return True

    async def _release_work(self, session, work_line_id, arrival_id):
        return await session.scalar(
            select(AutoBinWork)
            .where(AutoBinWork.work_line_id == work_line_id, AutoBinWork.arrival_id == arrival_id)
            .with_for_update()
        )

    async def start_release(self, session, *, work_line_id, arrival_id, now_ms):
        work = await self._release_work(session, work_line_id, arrival_id)
        if work is None or self.roller is None or not await self._bin_terminal(session, work):
            return "WAITING"
        if work.release_command_code is None:
            if not await matching_arrival(self.roller, session, work):
                return "WAITING"
            if work.association == "NO_TASK":
                siblings = await self.original_inprocess(
                    session, work_line_id=work_line_id, bin_code=work.bin_code
                )
                if any(row.id != work.id for row in siblings):
                    return "WAITING"
            work.release_decided_at_ms = now_ms
            work.release_command_code = await self.roller.release_scan2(
                session, arrival_id=arrival_id
            )
            await session.flush()
        return "RELEASING"

    async def consume_release(self, session, *, work_line_id, arrival_id, now_ms):
        work = await self._release_work(session, work_line_id, arrival_id)
        if work is None or self.roller is None or work.release_command_code is None:
            return "WAITING"
        view = await self.roller.get_scan2_release_result(session, arrival_id=arrival_id)
        result = self._release_success(work, view)
        if result is None or not await self._bin_terminal(session, work):
            return "RELEASING"
        if work.release_consumed_at_ms is None:
            work.release_result_ref = canonical_digest(result.model_dump())
            work.release_result_snapshot = result.model_dump()
            work.release_consumed_at_ms = now_ms
            work.pending = False
        return "RELEASED"

    async def release_bin(self, session, *, work_line_id, arrival_id, now_ms):
        await lock_line(session, self.dispatch, work_line_id)
        state = await self.start_release(
            session, work_line_id=work_line_id, arrival_id=arrival_id, now_ms=now_ms
        )
        if state == "WAITING":
            return state
        return await self.consume_release(
            session, work_line_id=work_line_id, arrival_id=arrival_id, now_ms=now_ms
        )

    async def _basis(self, session, task, now_ms):
        current = await RollerPorts().read_current_task(session, work_line_id=task.work_line_id)
        if current.task_id != task.task_id or not current.is_valid:
            return None
        return await self._obligation_basis(session, task, now_ms)

    async def _obligation_basis(self, session, task, now_ms):
        if not await RollerPorts.prepared(session, task):
            return None
        inputs = tuple(
            await session.scalars(
                select(AutoTaskInput)
                .where(AutoTaskInput.task_id == task.task_id)
                .order_by(AutoTaskInput.id)
            )
        )
        applied = sorted(
            r.applied_plan_revision for r in inputs if r.applied_plan_revision is not None
        )
        if applied != list(range(1, task.plan_revision + 1)) or any(
            r.disposition == "BLOCKED" for r in inputs
        ):
            return None
        if (
            task.plan_revision == 0
            and task.status != "CANCELLED"
            and (
                task.first_plan_wait_ms is None
                or task.first_plan_wait_ms <= 0
                or task.first_plan_deadline_ms is None
                or task.first_plan_deadline_ms
                != task.prepare_accepted_at_ms + task.first_plan_wait_ms
                or now_ms < task.first_plan_deadline_ms
            )
        ):
            return None
        inbound_plates = list(
            await session.scalars(
                select(AutoInboundPlate).where(AutoInboundPlate.task_id == task.task_id)
            )
        )
        outbound_plates = list(
            await session.scalars(
                select(AutoOutboundPlate).where(AutoOutboundPlate.task_id == task.task_id)
            )
        )
        for direction, plates, reader in (
            ("inbound", inbound_plates, self.inbound),
            ("outbound", outbound_plates, self.outbound),
        ):
            if plates and (
                task.business_type != direction
                or reader is None
                or any([not await reader.closed(session, plate) for plate in plates])
            ):
                return None
        if task.business_type == "inbound":
            members = list(
                await session.scalars(
                    select(AutoInboundSourceMember).where(
                        AutoInboundSourceMember.task_id == task.task_id
                    )
                )
            )
            expected = {
                (r.id, r.applied_plan_revision, index, source["rack_id"])
                for r in inputs
                if r.applied_plan_revision is not None
                for index, source in enumerate(r.effective_request.get("added_source_racks", []))
            }
            actual = {
                (m.plan_input_id, m.plan_revision, m.rack_array_index, m.rack_id) for m in members
            }
            if actual != expected or len(actual) != len(members):
                return None
        outbound_members = tuple(
            await session.scalars(
                select(AutoOutboundSourceMember).where(
                    AutoOutboundSourceMember.task_id == task.task_id
                )
            )
        )
        expected_faces = {
            (r.id, r.applied_plan_revision, index, rack["rack_id"], face)
            for r in inputs
            if r.applied_plan_revision is not None
            for index, rack in enumerate(r.effective_request.get("added_return_racks", []))
            for face in rack["rack_faces"]
        }
        actual_faces = {
            (m.plan_input_id, m.plan_revision, m.rack_array_index, m.rack_id, m.rack_face)
            for m in outbound_members
        }
        if expected_faces != actual_faces or len(actual_faces) != len(outbound_members):
            return None
        closed_faces = set()
        for model in (AutoInboundSourceMember, AutoOutboundSourceMember):
            for member in await session.scalars(select(model).where(model.task_id == task.task_id)):
                if (
                    model is AutoInboundSourceMember
                    and self.inbound is not None
                    and member.rack_job_id is not None
                    and member.preparation_action_id is not None
                    and await self.inbound.work_plans.inbound_source_exhausted(
                        session,
                        task_id=task.task_id,
                        job_id=member.rack_job_id,
                        preparation_action_id=member.preparation_action_id,
                    )
                ):
                    continue
                if (
                    model is AutoOutboundSourceMember
                    and self.outbound is not None
                    and member.rack_job_id is not None
                    and member.preparation_action_id is not None
                    and await self.outbound.face_closed(session, member)
                ):
                    closed_faces.add(member.preparation_action_id)
                    continue
                from .rack_ports import AutoRackPorts

                withdrawn = AutoRackPorts._withdrawn(member, inputs)
                if (
                    model is AutoInboundSourceMember
                    and withdrawn
                    and member.rack_job_id is not None
                ):
                    progress = (
                        await self.dispatch.progress(session, job_id=member.rack_job_id)
                        if self.dispatch
                        else None
                    )
                    action = (
                        next(
                            (a for a in progress.actions if a.id == member.preparation_action_id),
                            None,
                        )
                        if progress
                        else None
                    )
                    if (
                        progress is not None
                        and progress.origin.task_id == task.task_id
                        and progress.origin.rack_id == member.rack_id
                        and progress.origin.source_kind == "AUTO_INBOUND_SOURCE"
                        and self.inbound is not None
                        and await self.inbound.source_clear(session, job_id=progress.id)
                        and (
                            progress.state in ("WITHDRAWN", "NOT_ARRIVED")
                            or (
                                action is not None
                                and action.phase == "APPLIED"
                                and action.outcome
                                and action.outcome.get("status") == "SUCCEEDED"
                                and action.actual_position
                                == action.target
                                == {"kind": "RACK_POSITION", "location_code": progress.origin.point}
                            )
                        )
                    ):
                        continue
                if model is AutoOutboundSourceMember:
                    if await withdrawn_outbound_source(
                        session,
                        dispatch=self.dispatch,
                        task=task,
                        member=member,
                        inputs=inputs,
                        plates=outbound_plates,
                    ):
                        continue
                    return None
                if (
                    not withdrawn
                    or member.withdrawn_by_input_id is None
                    or member.withdrawn_at_ms is None
                    or member.rack_job_id is not None
                    or member.preparation_action_id is not None
                    or (
                        model is AutoOutboundSourceMember
                        and member.first_work_plan_round_id is not None
                    )
                ):
                    return None
        supplies = tuple(
            await session.scalars(
                select(AutoSupplyMember).where(AutoSupplyMember.task_id == task.task_id)
            )
        )
        expected_supply = {
            (
                r.id,
                r.applied_plan_revision,
                rack_index,
                face_index,
                rack["rack_id"],
                face,
                r.operation_id,
            )
            for r in inputs
            if r.applied_plan_revision is not None
            for rack_index, rack in enumerate(r.effective_request.get("added_bin_source_racks", []))
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
        if expected_supply != actual_supply or len(actual_supply) != len(supplies):
            return None
        if supplies and self.roller is None:
            return None
        progress = (
            ()
            if self.roller is None
            else await self.roller.get_supply_progress(
                session, work_line_id=task.work_line_id, task_id=task.task_id
            )
        )
        expected = {SourceFaceRef(m.plan_revision, m.rack_id, m.rack_face) for m in supplies}
        if any(p.task_id != task.task_id or p.source not in expected for p in progress) or len(
            {p.source for p in progress}
        ) != len(progress):
            return None
        for member in supplies:
            source = SourceFaceRef(member.plan_revision, member.rack_id, member.rack_face)
            facts = [p for p in progress if p.source == source]
            if not supply_member_closed(member, facts, inputs):
                return None
        bins = tuple(
            await session.scalars(
                select(AutoBinWork)
                .where(AutoBinWork.task_id == task.task_id)
                .order_by(AutoBinWork.id)
            )
        )
        if not supply_accounted_for(progress, production_count=len(bins)):
            return None
        if (
            task.status != "CANCELLED"
            and task.business_type == "outbound"
            and any(r.effective_request.get("target_rack") for r in inputs)
            and not (supplies or outbound_members or bins)
        ):
            return None
        for work in bins:
            if (
                work.work_line_id != task.work_line_id
                or not await self._bin_terminal(session, work)
                or self.roller is None
                or work.release_decided_at_ms is None
                or not await release_intent(
                    self.roller,
                    session,
                    arrival=Scan2Arrival(
                        work.arrival_id,
                        work.work_line_id,
                        work.device_code,
                        work.raw_bin_code,
                        work.bin_code,
                        work.scanned_at,
                    ),
                    task_id=work.task_id,
                    command_code=work.release_command_code,
                )
            ):
                return None
        for model in (AutoInboundDecisionRound, AutoOutboundDecisionRound):
            rounds = tuple(
                await session.scalars(select(model).where(model.task_id == task.task_id))
            )
            for round_ in rounds:
                if round_.unresolved:
                    return None
                if model is AutoInboundDecisionRound and round_.plate_id is not None:
                    if round_.plate_id not in {p.id for p in inbound_plates}:
                        return None
                elif model is AutoOutboundDecisionRound and round_.plate_id is not None:
                    if round_.plate_id not in {p.id for p in outbound_plates}:
                        return None
                elif round_.bin_work_id not in {b.id for b in bins} and (
                    model is not AutoOutboundDecisionRound
                    or round_.source_preparation_action_id not in closed_faces
                ):
                    return None
        basis = {
            "plan_revision": task.plan_revision,
            "inputs": [r.operation_id for r in inputs],
            "bins": [[b.id, b.release_command_code] for b in bins],
            "supply": [
                [p.source.plan_revision, p.source.rack_id, p.source.rack_face, list(p.original_ids)]
                for p in progress
            ],
        }
        # Keep already-saved Result-based confirmations recoverable across this change.
        legacy = {**basis, "bins": [[b.id, b.release_result_ref] for b in bins]}
        if task.confirmation_basis == legacy:
            for work in bins:
                if not await self._released(session, work):
                    return None
            return legacy
        return basis

    async def _released_bins(self, session, task):
        bins = tuple(
            await session.scalars(select(AutoBinWork).where(AutoBinWork.task_id == task.task_id))
        )
        for work in bins:
            if not await self._released(session, work):
                return None
        return bins

    async def cancelled_retention_closed_at(self, session, task, *, now_ms):
        requests = tuple(
            await session.scalars(
                select(AutoTaskRequest).where(AutoTaskRequest.task_id == task.task_id)
            )
        )
        if (
            task.status != "CANCELLED"
            or len(requests) != 1
            or requests[0].purpose != "PREPARE"
            or not await self._obligation_basis(session, task, now_ms)
        ):
            return None
        bins = await self._released_bins(session, task)
        if bins is None:
            return None
        prepare = requests[0]
        result = await self.requests.get_result(
            session,
            message_id=prepare.message_id,
            operation=prepare.operation,
            operation_id=prepare.operation_id,
        )
        if (
            (result.message_id, result.operation, result.operation_id)
            != (prepare.message_id, prepare.operation, prepare.operation_id)
            or result.availability != "FOUND"
            or result.response_state != "VALID"
            or prepare.response_snapshot.get("data") != result.response_data
        ):
            return None
        try:
            PrepareAccepted.parse(result.http_status, result.response_code, result.response_data)
        except (ValueError, TypeError):
            return None
        return max(
            task.closed_at_ms, prepare.consumed_at_ms, *(b.release_consumed_at_ms for b in bins)
        )

    async def retention_basis(self, session, task, *, now_ms):
        """Original completion and obligations, independent of present line configuration."""
        if task.status != "COMPLETED" or task.closed_at_ms is None:
            return None
        if await self._released_bins(session, task) is None:
            return None
        requests = list(
            await session.scalars(
                select(AutoTaskRequest)
                .where(
                    AutoTaskRequest.task_id == task.task_id, AutoTaskRequest.purpose == "COMPLETION"
                )
                .order_by(AutoTaskRequest.id)
            )
        )
        if not requests or any(r.unresolved or r.consumed_at_ms is None for r in requests):
            return None
        original = requests[-1]
        if (
            original.operation != "sorting.task.completion_confirm@v1"
            or original.requested_plan_revision != task.plan_revision
            or original.request_data
            != dict(task_id=task.task_id, last_applied_plan_revision=task.plan_revision)
        ):
            return None
        result = await self.requests.get_result(
            session,
            message_id=original.message_id,
            operation=original.operation,
            operation_id=original.operation_id,
        )
        if (
            (result.message_id, result.operation, result.operation_id)
            != (original.message_id, original.operation, original.operation_id)
            or result.availability != "FOUND"
            or result.response_state != "VALID"
            or result.http_status != 200
            or result.response_code != "DECIDED"
            or result.response_data != {"result": "COMPLETED"}
            or original.response_snapshot
            != dict(
                http_status=200,
                code="DECIDED",
                timestamp=result.response_timestamp,
                data=result.response_data,
            )
        ):
            return None
        basis = await self._obligation_basis(session, task, now_ms)
        return basis if basis is not None and basis == task.confirmation_basis else None

    async def advance(self, session, *, task_id, now_ms, allow_new=True):
        task = await session.get(AutoTask, task_id)
        if task is None:
            return "NOT_FOUND"
        await lock_line(session, self.dispatch, task.work_line_id)
        await session.refresh(task)
        if task.status in {"COMPLETED", "CANCELLED"}:
            return task.status
        confirmations = tuple(
            await session.scalars(
                select(AutoTaskRequest)
                .where(AutoTaskRequest.task_id == task_id, AutoTaskRequest.purpose == "COMPLETION")
                .order_by(AutoTaskRequest.id)
            )
        )
        pending = [r for r in confirmations if r.unresolved]
        if len(pending) > 1:
            return "CONFLICT"
        if pending:
            original = pending[0]
            if (
                original.operation != "sorting.task.completion_confirm@v1"
                or original.request_data
                != {
                    "task_id": task_id,
                    "last_applied_plan_revision": original.requested_plan_revision,
                }
            ):
                return "CONFIRMING"
            result = await self.requests.get_result(
                session,
                message_id=original.message_id,
                operation=original.operation,
                operation_id=original.operation_id,
            )
            if (
                (result.message_id, result.operation, result.operation_id)
                != (original.message_id, original.operation, original.operation_id)
                or result.availability != "FOUND"
                or result.response_state != "VALID"
            ):
                return "CONFIRMING"
            try:
                decision = CompletionDecision.parse(
                    result.http_status, result.response_code, result.response_data
                )
            except (ValueError, TypeError):
                return "CONFIRMING"
            if (
                decision.result == "PLAN_REVISION_STALE"
                and decision.current_plan_revision <= original.requested_plan_revision
            ):
                return "CONFIRMING"
            original.response_snapshot = {
                "http_status": result.http_status,
                "code": result.response_code,
                "timestamp": result.response_timestamp,
                "data": result.response_data,
            }
            if original.response_recorded_at_ms is None:
                original.response_recorded_at_ms = now_ms
            basis = await self._obligation_basis(session, task, now_ms)
            if decision.result == "COMPLETED":
                if basis is None or basis != task.confirmation_basis:
                    return "CONFLICT"
                task.status, task.closed_at_ms, task.pending = "COMPLETED", now_ms, False
                task.direct_obligations_closed_at_ms = (
                    task.direct_obligations_closed_at_ms or now_ms
                )
            else:
                original.completion_result = decision.model_dump(exclude_none=True)
                original.retry_after_ms = decision.retry_after_ms
                original.next_eligible_at_ms = (
                    None if decision.retry_after_ms is None else now_ms + decision.retry_after_ms
                )
                task.confirmation_next_check_at_ms = original.next_eligible_at_ms
            original.consumed_at_ms, original.unresolved = now_ms, False
            await session.flush()
            if decision.result != "COMPLETED" and allow_new:
                await self.resume_held(session, task_id=task_id, now_ms=now_ms)
            return task.status if decision.result == "COMPLETED" else decision.result
        if not allow_new:
            return "WAITING"
        await self.resume_held(session, task_id=task_id, now_ms=now_ms)
        basis = await self._basis(session, task, now_ms)
        if basis is None:
            return "WAITING"
        if confirmations:
            last = confirmations[-1]
            decision = last.completion_result or {}
            if (
                decision.get("result") == "PLAN_REVISION_STALE"
                and task.plan_revision < decision["current_plan_revision"]
            ):
                return "WAITING"
            if (
                last.next_eligible_at_ms is not None
                and now_ms < last.next_eligible_at_ms
                and basis == task.confirmation_basis
            ):
                return "WAITING"
        operation_id = str(uuid7())
        payload = CompletionConfirm(task_id=task_id, last_applied_plan_revision=task.plan_revision)
        message = await self.requests.send(
            session, payload, operation_id=operation_id, scope=ScopeRef("smt_auto", "TASK", task_id)
        )
        session.add(
            AutoTaskRequest(
                task_id=task_id,
                purpose="COMPLETION",
                operation="sorting.task.completion_confirm@v1",
                operation_id=operation_id,
                message_id=message.message_id,
                requested_plan_revision=task.plan_revision,
                request_data=payload.model_dump(mode="json"),
            )
        )
        task.confirmation_basis = basis
        await session.flush()
        return "CONFIRMING"

    async def resume_held(self, session, *, task_id, now_ms):
        if self.work_plans is None:
            return
        task = await session.get(AutoTask, task_id)
        if task is None or task.status not in {"PREPARING", "EXECUTING"}:
            return
        requests = tuple(
            await session.scalars(
                select(AutoTaskRequest)
                .where(AutoTaskRequest.task_id == task_id, AutoTaskRequest.purpose == "COMPLETION")
                .order_by(AutoTaskRequest.id)
            )
        )
        if (
            not requests
            or any(r.unresolved for r in requests)
            or (requests[-1].completion_result or {}).get("result")
            not in {"PLAN_REVISION_STALE", "BUSINESS_IN_PROGRESS"}
        ):
            return
        decision = requests[-1].completion_result
        if (
            decision.get("result") == "PLAN_REVISION_STALE"
            and task.plan_revision < decision["current_plan_revision"]
        ):
            return
        current = await RollerPorts().read_current_task(session, work_line_id=task.work_line_id)
        if not current.is_valid or current.task_id != task_id:
            return
        for work in await session.scalars(
            select(AutoBinWork)
            .where(
                AutoBinWork.task_id == task_id,
                AutoBinWork.production_hold_reason == "CONFIRMATION_PENDING",
            )
            .order_by(AutoBinWork.id)
        ):
            if (
                work.work_line_id != task.work_line_id
                or work.business_type != task.business_type
                or work.association != "TASK"
            ):
                continue
            work.production_hold_reason = None
            await session.flush()
            first = await self.work_plans.start_bin(
                session, work_line_id=task.work_line_id, arrival_id=work.arrival_id, now_ms=now_ms
            )
            if first is None:
                work.production_hold_reason = "CONFIRMATION_PENDING"
