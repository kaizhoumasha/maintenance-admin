"""Own each reel's facts and next intent, using the public integration services."""

import logging
from datetime import UTC, datetime
from functools import partial
from random import choice

from pydantic import ValidationError
from sqlalchemy import or_, select, tuple_
from sqlalchemy.exc import DBAPIError

from app.businesses._components.rack_dispatch.service import XRAY_SOURCES
from app.businesses.runtime import Capabilities
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import EventApplicationRef
from app.integrations.ecs_command.service import CommandOwner
from app.integrations.wms_request.adapter import MOCK_DECISIONS_TARGET
from app.models import Device, WorkLine
from app.reliable.lifecycle import EvidencePersistenceError, database_failure_code
from app.services.workline_service import business_configuration_issue
from app.transactions import after_commit
from app.xray_dispatch_trace import current_trace

from .contracts import DECISIONS
from .definition import DEFINITION, OUTPUT_ARM, XRAY
from .ecs_command import (
    ApplyCountDecision,
    ApplyPkgDecision,
    ApplyVerifyDecision,
    PutToBin,
    PutToRack,
)
from .evidence import (
    command_evidence,
    event_actions,
    fact_conflict,
    failed,
    ng_conflicts,
    select_events,
    succeeded,
    terminal,
)
from .models import RackPreparation, Reel, ReelEvent, ReelOperation

OWNER = CommandOwner(DEFINITION.code)
COMMANDS = {"ENTRY": ApplyPkgDecision, "COUNT": ApplyCountDecision, "VERIFY": ApplyVerifyDecision}
TIMEOUTS = {"COUNT_REQUIRED": 70000, "BYPASS_COUNT": 25000, "NG": 20000}
MOCK_FACTS_TARGET = "xray-mock-wms-facts"
logger = logging.getLogger(__name__)


class XrayService:
    def __init__(
        self, capabilities: Capabilities, transit_targets, requests, racks, mock_worklines=()
    ):
        self.capabilities = capabilities
        self.transit_targets = transit_targets
        self.requests = requests
        self.racks = racks
        self.mock_worklines = frozenset(mock_worklines)
        self.components = {"rack_dispatch": racks.dispatch}
        self.clock = lambda: datetime.now(UTC)

    async def accept(self, ctx, stage, payload):
        session = ctx.session
        issue = await business_configuration_issue(
            session,
            ctx.binding,
            OWNER.domain,
            definition_lookup=lambda _: DEFINITION,
            required_device_roles=(XRAY,),
        )
        if issue:
            ctx.note(issue)
            return
        # The receiver already holds the WorkLine and the original Device locks.
        line = await session.get(WorkLine, ctx.work_line_id)
        reel = await session.scalar(
            select(Reel)
            .where(
                Reel.work_line_id == line.id,
                Reel.device_id == ctx.binding.device_id,
                Reel.reel_id == payload.reel_id,
            )
            .with_for_update()
        )
        if reel is None:
            reel = Reel(
                id=str(uuid7()),
                work_line_id=line.id,
                workline_code=line.line_code,
                device_id=ctx.binding.device_id,
                device_code=ctx.device_code,
                reel_id=payload.reel_id,
            )
            session.add(reel)
            await session.flush()
        data = payload.model_dump(mode="json", exclude_none=True)
        original = await session.scalar(
            select(ReelEvent)
            .where(ReelEvent.device_id == reel.device_id, ReelEvent.event_id == payload.event_id)
            .order_by(ReelEvent.receipt_id)
            .limit(1)
        )
        canonical = ctx.receipt_id
        if original is not None:
            if (original.reel_key, original.stage, original.timestamp, original.payload) == (
                reel.id,
                stage,
                ctx.timestamp,
                data,
            ):
                canonical = original.receipt_id
            else:
                reel.conflict = "EVENT_IDENTITY_CONFLICT"
                if original.work_line_id == line.id and original.reel_key != reel.id:
                    other = await session.get(Reel, original.reel_key, with_for_update=True)
                    other.conflict = reel.conflict
                    other.pending = True
                    other.next_check_at_ms = 0
                ctx.note(reel.conflict)
        session.add(
            ReelEvent(
                receipt_id=ctx.receipt_id,
                reel_key=reel.id,
                work_line_id=line.id,
                device_id=reel.device_id,
                event_id=payload.event_id,
                stage=stage,
                timestamp=ctx.timestamp,
                payload=data,
            )
        )
        reel.pending = True
        reel.next_check_at_ms = 0
        ctx.bind_application(EventApplicationRef("xray_reel", reel.id, canonical))

    async def scan_pending(self, *, limit=100):
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        return (
            await self._scan(RackPreparation, self._advance_preparation, limit)
            + await self._scan(Reel, self._advance, limit)
            + await self._scan_rack_jobs(limit)
        )

    async def _scan_rack_jobs(self, limit):
        dispatch = self.racks.dispatch
        factory = self.capabilities.session_factory
        async with factory() as session:
            upper = await dispatch.pending_upper_id(session, source_kinds=XRAY_SOURCES)
        cursor, scanned = None, 0
        while upper is not None:
            async with factory() as session:
                ids = await dispatch.pending_ids(
                    session, source_kinds=XRAY_SOURCES, upper_id=upper, after_id=cursor, limit=limit
                )
            if not ids:
                break
            for identity in ids:
                await self._transaction(self._monitor_rack_job, identity)
            cursor = ids[-1]
            scanned += len(ids)
        return scanned

    async def _monitor_rack_job(self, session, identity):
        dispatch = self.racks.dispatch
        job = await dispatch.progress(session, job_id=identity)
        if job is None or not job.pending:
            return
        _, _, _, point = await self._lock_line(session, job.origin.work_line_id)
        if (
            point
            and point.latest
            and point.latest.pending
            and point.latest.point == job.origin.point
        ):
            return
        await self.racks.observe(session, job, unix_ms(self.clock()))

    async def _scan(self, table, advance, limit):
        factory = self.capabilities.session_factory
        due = table.next_check_at_ms
        cutoff = unix_ms(self.clock())
        eligible = table.pending & (due <= cutoff)
        async with factory() as session:
            upper = await session.scalar(
                select(table.id).where(eligible).order_by(table.id.desc()).limit(1)
            )
        if upper is None:
            return 0
        cursor, scanned = None, 0
        while True:
            query = select(due, table.id).where(eligible, table.id <= upper)
            if cursor is not None:
                query = query.where(tuple_(due, table.id) > cursor)
            async with factory() as session:
                page = (await session.execute(query.order_by(due, table.id).limit(limit))).all()
            if not page:
                return scanned
            for _, identity in page:
                await self._transaction(advance, identity)
            cursor = tuple(page[-1])
            scanned += len(page)

    async def advance(self, identity):
        return await self._transaction(self._advance, identity)

    async def _transaction(self, advance, identity):
        for attempt in range(3):
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    return await advance(session, identity)
            except (DBAPIError, EvidencePersistenceError) as error:
                if database_failure_code(error) != "40001":
                    raise
                if attempt == 2:
                    return "RETRY_LATER"

    async def _advance_preparation(self, session, identity):
        line_id = await session.scalar(
            select(RackPreparation.work_line_id).where(RackPreparation.id == identity)
        )
        if line_id is None:
            return "NOT_FOUND"
        _, _, _, point = await self._lock_line(session, line_id)
        prep = await session.get(RackPreparation, identity, with_for_update=True)
        if not prep or not prep.pending:
            return "COMPLETE"
        now_ms = unix_ms(self.clock())
        if prep.next_check_at_ms is None or prep.next_check_at_ms > now_ms:
            return "NOT_DUE"
        return await self.racks.advance(session, prep, point, now_ms)

    async def _lock_line(self, session, line_id):
        line = await session.get(WorkLine, line_id, with_for_update=True)
        codes = line.device_bindings if line else {}
        devices = {
            device.device_code: device
            for device in await session.scalars(
                select(Device)
                .where(
                    Device.work_line_id == line_id,
                    Device.device_code.in_(
                        [
                            code
                            for code in (codes.get(XRAY), codes.get(OUTPUT_ARM))
                            if code is not None
                        ]
                    ),
                )
                .order_by(Device.id)
                .with_for_update()
            )
        }
        arm = devices.get(codes.get(OUTPUT_ARM))
        point = await self.racks.lock_point(session, line, arm)
        return line, devices, arm, point

    async def _advance(self, session, identity):
        line_id = await session.scalar(select(Reel.work_line_id).where(Reel.id == identity))
        if line_id is None:
            return "NOT_FOUND"
        line, devices, arm, point = await self._lock_line(session, line_id)
        reel = await session.get(Reel, identity, with_for_update=True)
        if not reel.pending:
            return reel.wait_reason
        if reel.next_check_at_ms is None or reel.next_check_at_ms > unix_ms(self.clock()):
            return "NOT_DUE"
        reel.next_check_at_ms = 0
        original = devices.get(reel.device_code)
        configured = (
            line is not None
            and line.is_enabled
            and line.business_code == OWNER.domain
            and line.line_code == reel.workline_code
            and line.device_bindings.get(XRAY) == reel.device_code
            and original is not None
            and original.id == reel.device_id
        )

        prior_reason = reel.wait_reason
        reel.wait_reason = await self._progress(session, reel, arm, point, configured=configured)
        if reel.wait_reason != prior_reason and reel.wait_reason in {
            "OUTLET_WAIT",
            "OUTLET_DECISION_PENDING",
            "ARM_UNAVAILABLE",
            "ARM_BUSY",
            "TRANSIT_TARGETS_MISSING",
            "RACK_READINESS_REQUIRED",
            "RACK_POINT_UNCONFIGURED",
            "RACK_POINT_MISMATCH",
            "RACK_CONFIGURATION_CHANGED",
            "RACK_POSITION_CONFLICT",
            "RACK_SLOT_MISMATCH",
            "FIXED_TEST_RACK_TARGET_MISMATCH",
            "PUT_UNRESOLVED",
        }:
            logger.warning(
                "X-Ray outlet waiting",
                extra={
                    "workline_code": reel.workline_code,
                    "xray_reel_key": reel.id,
                    "wait_reason": reel.wait_reason,
                },
            )
        return reel.wait_reason

    async def _progress(self, session, reel, arm, point, *, configured):
        events = list(
            await session.scalars(
                select(ReelEvent)
                .where(ReelEvent.reel_key == reel.id)
                .order_by(ReelEvent.receipt_id)
            )
        )
        facts, requests, conflict = select_events(events)
        actions = reel.actions
        views = {
            stage: await self.capabilities.ecs.get_command(
                session, owner=OWNER, command_code=action["command_code"]
            )
            for stage, action in actions.items()
        }
        report = await self._latest(session, reel, "REPORT")
        if report is not None and report.response is None:
            await self._read_response(session, report)

        reel.conflict = reel.conflict or conflict or command_evidence(reel, actions, views)
        reason = await self._advance_stage(
            session, reel, arm, point, configured, facts, requests, views, report
        )
        if not reel.pending or (
            reel.conflict
            and (report is None or report.response is not None)
            and not await self._awaiting_command_results(session, views)
        ):
            reel.next_check_at_ms = None
        return reason

    async def _advance_stage(
        self, session, reel, arm, point, configured, facts, requests, views, report
    ):
        actions = reel.actions
        if "ENTRY" not in facts:
            return "ENTRY_UNAVAILABLE"
        reel.conflict = reel.conflict or fact_conflict(facts, actions, views)
        if reel.conflict:
            return reel.conflict
        for stage in set(event_actions(facts)):
            view = views.get(stage)
            if view and view.delivery_status in {"EXPIRED", "FAILED", "ABANDONED"}:
                if await self.capabilities.ecs.is_delivery_proven_unaccepted(
                    session, owner=OWNER, command_code=view.command_code
                ):
                    reel.conflict = "EVENT_FOR_UNACCEPTED_COMMAND"
                    return reel.conflict
        if await self._confirm_destination(session, reel, facts, actions, views, report):
            return reel.conflict or ("COMPLETE" if not reel.pending else "DESTINATION_CONFIRMED")
        if not configured:
            return "ORIGINAL_BINDING_UNAVAILABLE"
        if "NG" in actions:
            return "NG_RESULT_PENDING"
        if facts["ENTRY"].payload["scan_status"] == "NG":
            return "TECHNICAL_DISCHARGE_PENDING"

        stage = self._current_stage(facts, actions, views)
        if stage is None:
            return "STAGE_EVIDENCE_PENDING"
        source = facts[stage]
        request = requests.get(source.event_id)
        if request is not None and stage != "OUTLET":
            existing = actions.get(stage)
            if existing and not await self.capabilities.ecs.is_delivery_proven_unaccepted(
                session, owner=OWNER, command_code=existing["command_code"]
            ):
                return "NORMAL_COMMAND_UNRESOLVED"
            data = request.payload
            await self._ng(
                session, reel, stage, request, None, data["reason_code"], data["reason_message"]
            )
            return "NG_RESULT_PENDING"
        if stage in actions:
            return "PUT_UNRESOLVED" if stage == "OUTLET" else "STAGE_EVIDENCE_PENDING"

        intent = await self._latest(session, reel, stage)
        if intent is None:
            return await self._request_decision(session, reel, stage, source, intent, point)
        if intent.source_event_id != source.event_id:
            reel.conflict = "DECISION_SOURCE_CONFLICT"
            return reel.conflict
        if intent.response is None:
            await self._read_response(session, intent)
        if intent.response is None:
            return "OUTLET_DECISION_PENDING" if stage == "OUTLET" else "DECISION_PENDING"
        decision = intent.response
        outcome = decision["result"]
        if outcome == "WAIT":
            if intent.retry_at is not None:
                if intent.retry_at <= unix_ms(self.clock()):
                    return await self._request_decision(session, reel, stage, source, intent, point)
                if not await self._awaiting_command_results(session, views):
                    reel.next_check_at_ms = intent.retry_at
            return "OUTLET_WAIT" if stage == "OUTLET" else "BUSINESS_WAIT"
        if stage == "OUTLET":
            reason = await self._put(session, reel, arm, source, intent, decision, point)
            if reason == "RACK_PREPARATION_PENDING" and point and point.latest:
                due = point.latest.next_check_at_ms
                if (
                    due
                    and due > unix_ms(self.clock())
                    and not await self._awaiting_command_results(session, views)
                ):
                    reel.next_check_at_ms = due
            return reason
        if outcome == "NG":
            await self._ng(
                session,
                reel,
                stage,
                source,
                intent,
                decision["reason_code"],
                decision["reason_message"],
            )
            return "NG_RESULT_PENDING"
        if stage == "VERIFY":
            if source.payload["scan_status"] != "OK":
                reel.conflict = "VERIFY_ALLOW_WITHOUT_DUAL_BARCODE"
                return reel.conflict
            expected_spkg = actions["COUNT"]["params"]["label"]["variables"]["SPKG"]
            if source.payload["new_barcode"] != expected_spkg:
                reel.conflict = "VERIFY_ALLOW_WITH_MISMATCHED_SPKG"
                return reel.conflict
        params = {"decision": outcome}
        if stage == "COUNT":
            params["label"] = {"variables": decision["label_variables"]}
        timeout = (
            TIMEOUTS[outcome] if outcome in TIMEOUTS else (40000 if stage == "COUNT" else 25000)
        )
        await self._command(
            session, reel, stage, source, intent, reel.device_code, COMMANDS[stage], timeout, params
        )
        return "STAGE_EVIDENCE_PENDING"

    @staticmethod
    def _current_stage(facts, actions, views):
        if "ENTRY" not in actions:
            return "ENTRY"
        entry_action = actions["ENTRY"]["params"]["decision"]
        if entry_action == "BYPASS_COUNT":
            if "OUTLET" in facts and not failed(terminal(views, "ENTRY")):
                return "OUTLET"
            return None if terminal(views, "ENTRY") else "ENTRY"
        count = facts.get("COUNT")
        if count is None:
            return None if terminal(views, "ENTRY") else "ENTRY"
        if count.payload["count_status"] != "OK":
            return None
        if "COUNT" not in actions:
            return "COUNT"
        if "VERIFY" not in facts:
            return None if terminal(views, "COUNT") else "COUNT"
        if "VERIFY" not in actions:
            return "VERIFY"
        if "OUTLET" in facts and not failed(terminal(views, "VERIFY")):
            return "OUTLET"
        return None if terminal(views, "VERIFY") else "VERIFY"

    async def _confirm_destination(self, session, reel, facts, actions, views, report):
        ng_event = facts.get("NG")
        ng_result, put_result = terminal(views, "NG"), terminal(views, "OUTLET")
        ng_action = actions.get("NG")
        placed = succeeded(put_result)
        discharged = ng_event is not None or succeeded(ng_result)
        if discharged:
            stage = ng_event.payload["stage"] if ng_event else ng_action["phase"]
            if (
                placed
                or "OUTLET" in facts
                or ng_conflicts(stage, facts, actions, views)
                or (ng_event and succeeded(ng_result) and stage != ng_action["phase"])
                or (reel.destination and reel.destination != {"type": "NG", "stage": stage})
            ):
                reel.conflict = "DESTINATION_CONFLICT"
                return True
            destination = {"type": "NG", "stage": stage}
        elif placed:
            destination = actions["OUTLET"].get("rack_locator", {"type": "TRANSIT_BIN"})
            if reel.destination and reel.destination != destination:
                reel.conflict = "DESTINATION_CONFLICT"
                return True
            reel.arm_device_code = None
        else:
            return reel.destination is not None

        reel.destination = destination
        if report is None:
            base = {"workline_code": reel.workline_code}
            if discharged:
                data = ng_event.payload if ng_event else {}
                operation, model = self.requests["NG_REPORT"]
                payload = model(
                    **base,
                    stage=destination["stage"],
                    barcode=facts["ENTRY"].payload.get("barcode") or data.get("barcode"),
                    occurred_at=ng_event.timestamp if ng_event else ng_result["finish_time"],
                    reason_code=(
                        data.get("reason_code", "DEVICE_NG_DISCHARGED")
                        if ng_event
                        else ng_action["reason_code"]
                    ),
                    reason_message=data["reason"] if ng_event else ng_action["params"]["reason"],
                )
                await self._send(session, reel, "REPORT", operation, payload, ng_event)
            else:
                operation, model = self.requests["PUT_REPORT"]
                payload = model(
                    **base,
                    target_operation_id=actions["OUTLET"]["operation_id"],
                    to_locator=destination,
                    occurred_at=put_result["finish_time"],
                )
                await self._send(session, reel, "REPORT", operation, payload)
        else:
            reel.pending = report.response is None or await self._awaiting_command_results(
                session, views
            )
        return True

    async def _awaiting_command_results(self, session, views):
        for view in views.values():
            if view is None or view.result is not None:
                continue
            if view.delivery_status not in {"FAILED", "EXPIRED", "ABANDONED"}:
                return True
            if not await self.capabilities.ecs.is_delivery_proven_unaccepted(
                session, owner=OWNER, command_code=view.command_code
            ):
                return True
        return False

    async def _request_decision(self, session, reel, stage, source, intent, point):
        if intent and intent.source_event_id != source.event_id:
            reel.conflict = "DECISION_SOURCE_CONFLICT"
            return reel.conflict
        await self._send(
            session,
            reel,
            stage,
            self.requests[stage][0],
            self._decision_request(reel, stage, source),
            source,
            intent,
            rack_basis=point.request_basis if stage == "OUTLET" and point else None,
        )
        return "DECISION_PENDING"

    async def _put(self, session, reel, arm, source, intent, decision, point):
        context = {}
        if decision["result"] == "ACCEPT" and decision["target_locator"]["type"] == "TRANSIT_BIN":
            model = PutToBin
            targets = self.transit_targets.get(reel.workline_code)
            if targets is None:
                return "TRANSIT_TARGETS_MISSING"
            target = None
        else:
            reason, rack = await self.racks.target(
                session,
                reel,
                point,
                intent,
                unix_ms(self.clock()),
                barcode=source.payload["barcode"],
            )
            if reason == "RACK_REDECIDE":
                return await self._request_decision(session, reel, "OUTLET", source, intent, point)
            if rack is None:
                return reason
            target, context = rack
            model = PutToRack
        if arm is None:
            return "ARM_UNAVAILABLE"
        if await session.scalar(
            select(Reel.id)
            .where(
                Reel.arm_device_code.is_not(None),
                or_(
                    Reel.work_line_id == reel.work_line_id, Reel.arm_device_code == arm.device_code
                ),
                Reel.id != reel.id,
            )
            .limit(1)
        ):
            return "ARM_BUSY"
        if target is None:
            target = targets[choice(tuple(targets))].model_dump(mode="json")
        await self._command(
            session,
            reel,
            "OUTLET",
            source,
            intent,
            arm.device_code,
            model,
            90000,
            {"target": target},
            **context,
        )
        reel.arm_device_code = arm.device_code
        return "PUT_UNRESOLVED"

    async def _ng(self, session, reel, stage, source, intent, code, message):
        await self._command(
            session,
            reel,
            "NG",
            source,
            intent,
            reel.device_code,
            COMMANDS[stage],
            TIMEOUTS["NG"],
            {"decision": "NG", "reason": message},
            phase=stage,
            reason_code=code,
        )

    async def _command(
        self,
        session,
        reel,
        action,
        source,
        intent,
        device_code,
        model,
        timeout_ms,
        fields,
        **context,
    ):
        params = model(reel_id=reel.reel_id, source_event_id=source.event_id, **fields)
        call_key = f"{reel.id}:{action}"
        view = await self.capabilities.ecs.create(
            session,
            owner=OWNER,
            call_key=call_key,
            device_code=device_code,
            timeout_ms=timeout_ms,
            command=params,
        )
        reel.actions = {
            **reel.actions,
            action: {
                "command_code": view.command_code,
                "message_id": view.message_id,
                "call_key": call_key,
                "device_code": device_code,
                "params": params.model_dump(mode="json"),
                "operation_id": intent.operation_id if intent else None,
                **context,
            },
        }
        if trace := current_trace.get():
            after_commit(
                session,
                partial(
                    trace.log,
                    "ecs_command_registered",
                    wms_operation_id=intent.operation_id if intent else None,
                    command_code=view.command_code,
                ),
            )

    @staticmethod
    async def _latest(session, reel, stage):
        return await session.scalar(
            select(ReelOperation)
            .where(ReelOperation.reel_key == reel.id, ReelOperation.stage == stage)
            .order_by(ReelOperation.round.desc())
            .limit(1)
        )

    async def _send(
        self,
        session,
        reel,
        stage,
        operation,
        payload,
        source=None,
        previous=None,
        *,
        rack_basis=None,
    ):
        identity = str(uuid7())
        explicit_target = None
        if reel.workline_code in self.mock_worklines:
            explicit_target = MOCK_FACTS_TARGET if stage == "REPORT" else MOCK_DECISIONS_TARGET
        reference = await self.capabilities.wms.send(
            session, payload, operation_id=identity, explicit_target=explicit_target
        )
        session.add(
            ReelOperation(
                operation_id=identity,
                operation=operation,
                reel_key=reel.id,
                stage=stage,
                message_id=reference.message_id,
                round=previous.round + 1 if previous else 1,
                source_event_id=source.event_id if source else None,
                rack_basis=rack_basis,
            )
        )

    async def _read_response(self, session, intent):
        saved = await self.capabilities.wms.get_result(
            session,
            message_id=intent.message_id,
            operation=intent.operation,
            operation_id=intent.operation_id,
        )
        if saved.response_state != "VALID":
            return
        if intent.stage == "REPORT":
            if saved.response_code in ("RECORDED", "DUPLICATE") and saved.response_data == {}:
                intent.response = {"code": saved.response_code}
            return
        if saved.response_code != "DECIDED":
            return
        try:
            decision = DECISIONS[intent.stage].validate_python(saved.response_data)
        except ValidationError:
            return
        if decision.result == "WAIT":
            received_at = saved.response_received_at_ms
            if received_at is None:
                return
            intent.retry_at = received_at + decision.retry_after_ms
        intent.response = decision.model_dump(mode="json")

    def _decision_request(self, reel, stage, event):
        model = self.requests[stage][1]
        data = event.payload
        base = dict(
            workline_code=reel.workline_code,
            barcode=data["barcode"],
        )
        if stage == "ENTRY":
            return model(**base, scanned_at=event.timestamp)
        if stage == "COUNT":
            return model(
                **base,
                actual_qty=data["qty"],
                template_id=data["template_id"],
                observed_at=event.timestamp,
            )
        if stage == "VERIFY":
            return model(
                **base,
                scan_status=data["scan_status"],
                expected_spkg=reel.actions["COUNT"]["params"]["label"]["variables"]["SPKG"],
                new_barcode=data.get("new_barcode"),
                reason_code=data.get("reason_code"),
                reason_message=data.get("reason_message"),
                scanned_at=event.timestamp,
            )
        return model(
            **base,
            mode=data["mode"],
            new_barcode=data.get("new_barcode"),
            observed_at=event.timestamp,
        )
