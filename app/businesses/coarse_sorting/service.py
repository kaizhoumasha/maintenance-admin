"""Accept scan facts and advance their original validation and conveyor command."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import Table, or_, select
from sqlalchemy.exc import DBAPIError

from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.runtime import Capabilities, require_runtime
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest
from app.diagnostics import log_exception
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.ecs_callback.registry import EcsEventContext
from app.integrations.ecs_command import CommandOwner
from app.integrations.wms_request import RequestPayload
from app.models import Device, WorkLine
from app.services.workline_service import business_configuration_issue, read_workline

from .contracts import (
    ALLOCATE,
    REPORT,
    Allocated,
    Replace,
    Replenish,
    ScanCompletedData,
    ValidateBindDecision,
    Wait,
    allocation_decision,
)
from .definition import DEFINITION
from .ecs_command import MoveForward, MoveToLocationResult, MoveToNg, MoveToNgResult, PutToBin

logger = logging.getLogger(__name__)
OWNER = CommandOwner("coarse_sorting")
VALIDATE = "inbound.coarse_sorting.validate_bind@v1"
DIRECT_PUT_STATES = ("AWAITING_ALLOCATION", "AWAITING_RACK", "AWAITING_PUT", "AWAITING_REPORT")
WAIT_INTERVAL = timedelta(milliseconds=5000)


@dataclass(frozen=True)
class DirectPutContracts:
    allocate: type[RequestPayload]
    report: type[RequestPayload]
    output_locations: Mapping[str, str]


@dataclass(frozen=True)
class CoarseSortingService:
    scan_table: Table
    capabilities: Capabilities
    validate_bind: type[RequestPayload]
    action_table: Table | None = None
    direct_put_contracts: DirectPutContracts | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ng_locations: Mapping[str, str] | None = None
    preparation_table: Table | None = None
    rack_dispatch: RackDispatch | None = None

    @property
    def direct_put(self) -> bool:
        return self.direct_put_contracts is not None

    async def accept_scan(self, ctx: EcsEventContext, payload: ScanCompletedData) -> None:
        table = self.scan_table
        original = await ctx.session.scalar(
            select(table.c.id).where(table.c.event_digest == ctx.digest)
        )
        if original is not None:
            return

        binding = ctx.binding
        if binding.device_role != "INPUT_ARM":
            ctx.note("DEVICE_ROLE_MISMATCH")
            return
        runtime = require_runtime(ctx)
        issue = await business_configuration_issue(
            ctx.session,
            binding,
            "coarse_sorting",
            definition_lookup=runtime.definitions.__getitem__,
        )
        if issue:
            ctx.note(issue)
            return
        line = await read_workline(ctx.session, binding.work_line_id)
        await ctx.session.execute(
            table.insert().values(
                id=str(uuid7()),
                event_digest=ctx.digest,
                receipt_id=ctx.receipt_id,
                work_line_id=binding.work_line_id,
                workline_code=line.line_code,
                device_id=binding.device_id,
                device_code=binding.device_code,
                device_role=binding.device_role,
                device_version=binding.device_version,
                work_line_version=binding.work_line_version,
                binding_revision=binding.revision,
                timestamp=ctx.timestamp,
                payload={
                    "barcode": payload.barcode,
                    "reel_diameter": payload.size_inch,
                    "reel_thickness": payload.thickness_mm,
                },
                received_at=datetime.now(UTC),
                pending=True,
                wait_reason="AWAITING_VALIDATION_STAGE",
            )
        )

    async def advance(self, scan_id: str) -> str:
        """Retry serialization failures as whole transactions on fresh sessions."""
        for attempt in range(3):
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={
                                "isolation_level": "SERIALIZABLE"
                                if self.direct_put or self.ng_locations is not None
                                else "REPEATABLE READ"
                            }
                        )
                    return await self._advance(session, scan_id)
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def _advance(self, session, scan_id):
        table = self.scan_table
        query = select(table).where(table.c.id == scan_id)
        located = (await session.execute(query)).mappings().one_or_none()
        if located is None:
            return "NOT_FOUND"
        line = await session.scalar(
            select(WorkLine).where(WorkLine.id == located["work_line_id"]).with_for_update()
        )
        conveyor_code = located["conveyor_device_code"] or (
            line.device_bindings.get("CONVEYOR") if line else None
        )
        output_code = line.device_bindings.get("OUTPUT_ARM") if line and self.direct_put else None
        devices = list(
            await session.scalars(
                select(Device)
                .where(
                    or_(
                        Device.id == located["device_id"],
                        Device.id == located["conveyor_device_id"],
                        Device.device_code == conveyor_code,
                        Device.device_code == output_code if output_code else False,
                    )
                )
                .order_by(Device.id)
                .with_for_update()
            )
        )
        if self.preparation_table is not None:
            await self._lock_point(session, located["work_line_id"])
        row = (await session.execute(query.with_for_update())).mappings().one()

        async def save(reason, **values):
            values = {key: value for key, value in values.items() if row[key] != value}
            if values or row["wait_reason"] != reason:
                await session.execute(
                    table.update()
                    .where(table.c.id == scan_id)
                    .values(wait_reason=reason, updated_at=self.clock(), **values)
                )
            return reason

        if not row["pending"]:
            return row["wait_reason"]
        known_ng = (
            row["state"] == "AWAITING_NG"
            or row["wait_reason"] == "AWAITING_NG_STAGE"
            or row["ng_reason_code"] is not None
        )
        if known_ng and self.ng_locations is None:
            return row["wait_reason"]
        if not self.direct_put and row["state"] in DIRECT_PUT_STATES:
            return row["wait_reason"]
        if row["state"] == "AWAITING_RACK" and self.preparation_table is None:
            return row["wait_reason"]
        actions = await self._lock_actions(session, row)
        action = actions.get(row["current_action_id"])
        input_arm = next((d for d in devices if d.id == row["device_id"]), None)
        if known_ng:
            if action and action["kind"] == "MOVE_TO_NG":
                return await self._advance_ng_result(session, row, action, save)
            return await self._resume_ng_validation(session, row, action, line, input_arm, save)
        if self.direct_put and row["state"] == "AWAITING_PUT":
            return await self._advance_put(session, row, action, actions, save)
        if self.direct_put and row["state"] == "AWAITING_REPORT":
            return await self._advance_report(session, action, save)

        if not row["command_code"] and (
            row["state"] in {"AWAITING_MOVE", "AWAITING_ALLOCATION"}
            or row["binding_id"] is not None
            or row["move_result"] is not None
        ):
            return await save("MOVE_RECONCILIATION_REQUIRED", state="AWAITING_MOVE")
        issue, conveyor = await self._configuration_issue(
            session,
            row,
            line,
            devices,
            conveyor_code,
            first_allocation=(
                self.direct_put
                and row["command_code"] is not None
                and row["rack_binding_version"] is None
            ),
        )
        output = next((d for d in devices if d.device_code == output_code), None)
        if self.preparation_table is not None and row["state"] == "AWAITING_RACK":
            if issue:
                return await save(issue)
            return await self._resume_rack(session, row, action, line, output, save)
        if self.direct_put and row["state"] == "AWAITING_ALLOCATION":
            if action and action["kind"] == "ALLOCATE_STORAGE":
                return await self._advance_allocation(
                    session, row, action, line, output, issue, save
                )
        if issue and not (
            (self.direct_put and (row["command_code"] or row["validation_operation_id"]))
            or (
                self.ng_locations is not None
                and row["command_code"] is None
                and row["validation_operation_id"] is not None
            )
        ):
            return await save(issue)
        if row["command_code"] is not None:
            command = await self.capabilities.ecs.get_command(
                session, owner=OWNER, command_code=row["command_code"]
            )
            observed = asdict(command) if command else None
            if observed is None and self.direct_put and row["state"] == "AWAITING_ALLOCATION":
                observed = row["move_result"]
            if observed is None:
                return await save("MOVE_RECONCILIATION_REQUIRED")
            result = observed.get("result")
            if result is not None:
                if action and action["kind"] == "MOVE_FORWARD":
                    await self._consume(session, action, self._ecs_evidence(observed))
                if self._move_complete(observed, row):
                    if self.direct_put:
                        if (
                            action is None
                            or action["kind"] != "MOVE_FORWARD"
                            or action["command_owner"] != OWNER.domain
                            or action["command_code"] != row["command_code"]
                            or action["device_code"] != row["conveyor_device_code"]
                            or action["call_key"] != f"{scan_id}:MOVE_FORWARD"
                        ):
                            return await save("MOVE_RECONCILIATION_REQUIRED", move_result=observed)
                        if issue:
                            return await save(issue, move_result=observed)
                        if action["resource_snapshot"] != self._device_snapshot(
                            conveyor, row["work_line_version"], "CONVEYOR"
                        ):
                            return await save("MOVE_RECONCILIATION_REQUIRED", move_result=observed)
                        return await self._allocate(
                            session, row, action, line, output, save, move_result=observed
                        )
                    return await save(
                        "AWAITING_ALLOCATION_STAGE",
                        state="AWAITING_ALLOCATION",
                        move_result=observed,
                    )
                return await save("MOVE_RECONCILIATION_REQUIRED", move_result=observed)
            if observed["needs_reconciliation"] or observed["status"] in {
                "RECONCILING",
                "REJECTED",
                "EXPIRED",
                "FAILED",
            }:
                return await save("MOVE_RECONCILIATION_REQUIRED")
            return await save("AWAITING_MOVE_RESULT")
        if row["validation_operation_id"] is None:
            if row["wait_reason"] in {
                "AWAITING_VALIDATION_RESULT",
                "VALIDATION_RECONCILIATION_REQUIRED",
            } or any(
                row[key] is not None
                for key in (
                    "validation_message_id",
                    "validation_response",
                    "binding_id",
                    "current_action_id",
                )
            ):
                return await save("VALIDATION_RECONCILIATION_REQUIRED")
            payload = row["payload"]
            operation_id = str(uuid7())
            model = self.validate_bind(
                workline_code=row["workline_code"],
                barcode=payload["barcode"],
                reel_diameter=payload["reel_diameter"],
                reel_thickness=payload["reel_thickness"],
            )
            reference = await self.capabilities.wms.send(session, model, operation_id=operation_id)
            action_id = await self._insert_action(
                session,
                row,
                "VALIDATE_BIND",
                model.model_dump(),
                operation=VALIDATE,
                operation_id=operation_id,
                message_id=reference.message_id,
            )
            return await save(
                "AWAITING_VALIDATION_RESULT",
                current_action_id=action_id,
                validation_operation_id=operation_id,
                validation_message_id=reference.message_id,
                conveyor_device_id=conveyor.id,
                conveyor_device_code=conveyor.device_code,
                conveyor_device_version=conveyor.version,
            )
        if row["validation_message_id"] is None:
            return await save("VALIDATION_RECONCILIATION_REQUIRED")
        result = await self.capabilities.wms.get_result(
            session,
            message_id=row["validation_message_id"],
            operation=VALIDATE,
            operation_id=row["validation_operation_id"],
        )
        if not self._valid_response(result, "DECIDED"):
            return await save(self._request_wait_reason(result, "VALIDATION"))
        try:
            decision = ValidateBindDecision.model_validate(result.response_data)
        except ValidationError:
            return await save(
                "VALIDATION_RECONCILIATION_REQUIRED", validation_response=asdict(result)
            )
        if decision.result == "NG":
            evidence = self._wms_evidence(result)
            if self.ng_locations is not None:
                return await self._register_ng(
                    session,
                    row,
                    action,
                    line,
                    input_arm,
                    evidence,
                    save,
                    validation_response=asdict(result),
                )
            if action and action["kind"] == "VALIDATE_BIND":
                await self._consume(session, action, evidence)
            return await save(
                "AWAITING_NG_STAGE",
                state="AWAITING_NG",
                validation_response=asdict(result),
                ng_reason_code=decision.reason_code,
            )
        if action and action["kind"] == "VALIDATE_BIND":
            await self._consume(session, action, self._wms_evidence(result))
        if issue:
            return await save(issue, validation_response=asdict(result))
        model = MoveForward(barcode=row["payload"]["barcode"])
        call_key = f"{scan_id}:MOVE_FORWARD"
        command = await self.capabilities.ecs.create(
            session,
            owner=OWNER,
            call_key=call_key,
            device_code=conveyor.device_code,
            timeout_ms=30000,
            command=model,
        )
        action_id = await self._insert_action(
            session,
            row,
            "MOVE_FORWARD",
            model.model_dump(),
            predecessor=action,
            command_owner=OWNER.domain,
            call_key=call_key,
            command_code=command.command_code,
            device_code=conveyor.device_code,
            resource_snapshot=self._device_snapshot(conveyor, row["work_line_version"], "CONVEYOR"),
        )
        return await save(
            "AWAITING_MOVE_RESULT",
            state="AWAITING_MOVE",
            current_action_id=action_id,
            validation_response=asdict(result),
            binding_id=decision.binding_id,
            command_code=command.command_code,
        )

    async def _configuration_issue(
        self, session, row, line, devices, conveyor_code, *, first_allocation=False
    ):
        device = next((d for d in devices if d.id == row["device_id"]), None)
        conveyor = next((d for d in devices if d.device_code == conveyor_code), None)
        version = row["rack_binding_version"] or row["work_line_version"]
        binding = self._input_binding(row, line, device)
        if (
            binding is None
            or conveyor is None
            or (not first_allocation and line.version != version)
            or (
                first_allocation
                and (
                    line.version < version
                    or row["conveyor_device_id"] is None
                    or row["conveyor_device_version"] is None
                )
            )
            or conveyor.work_line_id != line.id
            or line.device_bindings.get("CONVEYOR") != conveyor.device_code
            or (
                row["conveyor_device_id"] is not None
                and (
                    conveyor.id != row["conveyor_device_id"]
                    or conveyor.version != row["conveyor_device_version"]
                )
            )
        ):
            return "ORIGINAL_BINDING_CHANGED", None
        if self.direct_put and not line.is_enabled:
            return "WORKLINE_DISABLED", None
        issue = await business_configuration_issue(
            session, binding, "coarse_sorting", definition_lookup=lambda _: DEFINITION
        )
        return issue, conveyor

    @staticmethod
    def _input_binding(row, line, device):
        if (
            line is None
            or device is None
            or line.line_code != row["workline_code"]
            or row["device_role"] != "INPUT_ARM"
            or device.version != row["device_version"]
            or device.device_code != row["device_code"]
            or device.work_line_id != line.id
            or line.device_bindings.get("INPUT_ARM") != device.device_code
        ):
            return None
        binding = DeviceBinding(
            device_id=device.id,
            device_code=device.device_code,
            device_version=device.version,
            work_line_id=line.id,
            work_line_version=line.version,
            device_role="INPUT_ARM",
            device_bindings=line.device_bindings,
            position_bindings=line.position_bindings,
        )
        original = replace(binding, work_line_version=row["work_line_version"])
        if original.revision != row["binding_revision"]:
            return None
        return binding

    @staticmethod
    def _move_complete(command, row):
        if not command or not isinstance(result := command.get("result"), dict):
            return False
        data = result.get("data")
        return (
            command.get("command_code") == row["command_code"]
            and command.get("task_type") == "MOVE_FORWARD"
            and command.get("device_code") == row["conveyor_device_code"]
            and command.get("status") == "SUCCEEDED"
            and command.get("needs_reconciliation") is False
            and result.get("command_code") == row["command_code"]
            and result.get("device_code") == row["conveyor_device_code"]
            and result.get("result") == "SUCCESS"
            and type(result.get("finish_time")) is int
            and 0 <= result["finish_time"] <= 2**63 - 1
            and isinstance(data, dict)
            and type(data.get("actual_qty")) is int
            and data["actual_qty"] == 1
        )

    @staticmethod
    def _device_snapshot(device, work_line_version, role):
        return {
            "device_id": device.id,
            "device_code": device.device_code,
            "device_version": device.version,
            "role": role,
            "work_line_version": work_line_version,
        }

    async def _lock_actions(self, session, row):
        table = self.action_table
        if table is None or row["current_action_id"] is None:
            return {}
        ids = [row["current_action_id"], row["allocation_action_id"]]
        result = await session.execute(
            select(table)
            .where(table.c.job_id == row["id"], table.c.id.in_(ids))
            .order_by(table.c.id)
            .with_for_update()
        )
        return {action["id"]: action for action in result.mappings()}

    async def _insert_action(
        self,
        session,
        row,
        kind,
        request_data,
        *,
        predecessor=None,
        action_id=None,
        round_no=1,
        **references,
    ):
        table = self.action_table
        if table is None:
            return None
        action_id = action_id or str(uuid7())
        await session.execute(
            table.insert().values(
                id=action_id,
                job_id=row["id"],
                kind=kind,
                round_no=round_no,
                predecessor_action_id=predecessor["id"] if predecessor else None,
                request_data=request_data,
                evidence_history=[],
                created_at=self.clock(),
                **references,
            )
        )
        return action_id

    async def _consume(self, session, action, evidence):
        history = action["evidence_history"]
        evidence = {key: value for key, value in evidence.items() if key != "consumed_at"}
        if any(
            {k: v for k, v in prior.items() if k != "consumed_at"} == evidence for prior in history
        ):
            return
        now = self.clock()
        table = self.action_table
        await session.execute(
            table.update()
            .where(table.c.id == action["id"])
            .values(
                evidence_history=[*history, {**evidence, "consumed_at": now.isoformat()}],
                last_consumed_at=now,
            )
        )

    @staticmethod
    def _ecs_evidence(command):
        return {
            "command_code": command["command_code"],
            "digest": canonical_digest(command["result"]),
            "data": command["result"],
        }

    @staticmethod
    def _wms_evidence(result):
        evidence = {
            "operation_id": result.operation_id,
            "code": result.response_code,
            "timestamp": result.response_timestamp,
            "data": result.response_data,
        }
        if result.operation == VALIDATE and result.response_data.get("result") == "NG":
            evidence.update(operation=result.operation, message_id=result.message_id)
        return evidence

    async def _send_request(self, session, row, predecessor, kind, operation, model, **values):
        operation_id = str(uuid7())
        reference = await self.capabilities.wms.send(session, model, operation_id=operation_id)
        return await self._insert_action(
            session,
            row,
            kind,
            model.model_dump(),
            predecessor=predecessor,
            operation=operation,
            operation_id=operation_id,
            message_id=reference.message_id,
            **values,
        )

    async def _read_request(self, session, action):
        return await self.capabilities.wms.get_result(
            session,
            message_id=action["message_id"],
            operation=action["operation"],
            operation_id=action["operation_id"],
        )

    @staticmethod
    def _request_wait_reason(result, phase):
        if (
            result.availability == "FOUND"
            and not result.delivery_terminal
            and not result.delivery_uncertain
        ):
            return f"AWAITING_{phase}_RESULT"
        return f"{phase}_RECONCILIATION_REQUIRED"

    @staticmethod
    def _valid_response(result, *codes):
        return (
            result.availability == "FOUND"
            and result.response_state == "VALID"
            and result.http_status == 200
            and result.response_code in codes
        )

    async def _resume_ng_validation(self, session, row, action, line, input_arm, save):
        if not action or action["kind"] != "VALIDATE_BIND" or not row["validation_message_id"]:
            return await save("NG_RECONCILIATION_REQUIRED")
        result = await self.capabilities.wms.get_result(
            session,
            message_id=row["validation_message_id"],
            operation=VALIDATE,
            operation_id=row["validation_operation_id"],
        )
        if self._valid_response(result, "DECIDED"):
            return await self._register_ng(
                session,
                row,
                action,
                line,
                input_arm,
                self._wms_evidence(result),
                save,
                validation_response=asdict(result),
            )
        if result.availability != "NOT_FOUND":
            return await save("NG_RECONCILIATION_REQUIRED")
        evidence = self._retained_validation_evidence(row, action)
        if evidence is None:
            return await save("NG_RECONCILIATION_REQUIRED")
        return await self._register_ng(session, row, action, line, input_arm, evidence, save)

    @staticmethod
    def _retained_validation_evidence(row, action):
        saved = row["validation_response"]
        if saved is None:
            history = action["evidence_history"]
            return history[-1] if history else None
        # A backfilled action cannot confer provenance the original snapshot lacks.
        if not (
            isinstance(saved, dict)
            and saved.get("availability") == "FOUND"
            and saved.get("operation") == VALIDATE
            and saved.get("operation_id") == row["validation_operation_id"]
            and saved.get("message_id") == row["validation_message_id"]
            and saved.get("response_state") == "VALID"
            and saved.get("http_status") == 200
            and saved.get("response_code") == "DECIDED"
        ):
            return None
        return {
            "operation": saved["operation"],
            "operation_id": saved["operation_id"],
            "message_id": saved["message_id"],
            "code": saved["response_code"],
            "timestamp": saved.get("response_timestamp"),
            "data": saved.get("response_data"),
        }

    async def _register_ng(self, session, row, action, line, input_arm, evidence, save, **values):
        if (
            not action
            or action["kind"] != "VALIDATE_BIND"
            or action["operation"] != VALIDATE
            or action["operation_id"] != row["validation_operation_id"]
            or action["message_id"] != row["validation_message_id"]
            or action["request_data"] != {"workline_code": row["workline_code"], **row["payload"]}
            or evidence.get("operation") != action["operation"]
            or evidence.get("operation_id") != action["operation_id"]
            or evidence.get("message_id") != action["message_id"]
            or evidence.get("code") != "DECIDED"
            or type(evidence.get("timestamp")) is not int
            or row["binding_id"] is not None
            or row["command_code"] is not None
        ):
            return await save("NG_RECONCILIATION_REQUIRED")
        try:
            decision = ValidateBindDecision.model_validate(evidence.get("data"))
        except ValidationError:
            return await save("NG_RECONCILIATION_REQUIRED")
        if decision.result != "NG" or row["ng_reason_code"] not in (None, decision.reason_code):
            return await save("NG_RECONCILIATION_REQUIRED")
        history = action["evidence_history"]
        if any(entry.get("data") != evidence["data"] for entry in history):
            return await save("NG_RECONCILIATION_REQUIRED")
        if not history:
            await self._consume(session, action, evidence)
        values["ng_reason_code"] = decision.reason_code

        issue = await self._ng_configuration_issue(session, row, line, input_arm)
        if issue:
            return await save(issue, **values)
        location = self.ng_locations.get(line.line_code)
        if not location:
            return await save("NG_PLATFORM_CONFIGURATION_REQUIRED", **values)
        try:
            model = MoveToNg(barcode=row["payload"]["barcode"], reason_code=decision.reason_code)
        except ValidationError:
            return await save("NG_RECONCILIATION_REQUIRED", **values)
        action_id = str(uuid7())
        command = await self.capabilities.ecs.create(
            session,
            owner=OWNER,
            call_key=action_id,
            device_code=input_arm.device_code,
            timeout_ms=30000,
            command=model,
        )
        await self._insert_action(
            session,
            row,
            "MOVE_TO_NG",
            model.model_dump(),
            predecessor=action,
            action_id=action_id,
            command_owner=OWNER.domain,
            call_key=action_id,
            command_code=command.command_code,
            device_code=input_arm.device_code,
            resource_snapshot={
                **self._device_snapshot(input_arm, line.version, "INPUT_ARM"),
                "completion_location": location,
            },
        )
        return await save(
            "AWAITING_NG_RESULT", state="AWAITING_NG", current_action_id=action_id, **values
        )

    async def _ng_configuration_issue(self, session, row, line, input_arm):
        binding = self._input_binding(row, line, input_arm)
        if (
            binding is None
            or binding.work_line_version != row["work_line_version"]
            or list(binding.device_bindings.values()).count(binding.device_code) != 1
        ):
            return "ORIGINAL_BINDING_CHANGED"
        if not line.is_enabled:
            return "WORKLINE_DISABLED"
        return await business_configuration_issue(
            session,
            binding,
            OWNER.domain,
            definition_lookup=lambda _: DEFINITION,
            required_device_roles=("INPUT_ARM",),
            required_position_slots=(),
        )

    async def _advance_ng_result(self, session, row, action, save):
        if (
            row["state"] != "AWAITING_NG"
            or not row["ng_reason_code"]
            or action["command_owner"] != OWNER.domain
            or action["call_key"] != action["id"]
            or action["device_code"] != row["device_code"]
            or action["request_data"]
            != {
                "barcode": row["payload"]["barcode"],
                "reason_code": row["ng_reason_code"],
            }
            or not isinstance(snapshot := action["resource_snapshot"], dict)
            or not snapshot.get("completion_location")
        ):
            return await save("NG_RECONCILIATION_REQUIRED")
        command = await self.capabilities.ecs.get_by_call(
            session, owner=OWNER, call_key=action["call_key"]
        )
        if (
            command is None
            or command.command_code != action["command_code"]
            or command.task_type != "MOVE_TO_NG"
            or command.device_code != action["device_code"]
        ):
            return await save("NG_RECONCILIATION_REQUIRED")
        result = command.result
        if result is None:
            if not command.needs_reconciliation and command.status in {
                "QUEUED",
                "DISPATCHING",
                "WAITING_RESULT",
            }:
                return await save("AWAITING_NG_RESULT")
            return await save("NG_RECONCILIATION_REQUIRED")
        if (
            result.get("command_code") != action["command_code"]
            or result.get("device_code") != action["device_code"]
        ):
            return await save("NG_RECONCILIATION_REQUIRED")
        await self._consume(session, action, self._ecs_evidence(asdict(command)))
        if (
            command.status != "SUCCEEDED"
            or command.delivery_status != "DONE"
            or command.needs_reconciliation
            or result.get("result") != "SUCCESS"
        ):
            return await save("NG_RECONCILIATION_REQUIRED")
        try:
            completed = MoveToNgResult.model_validate(result.get("data"))
        except ValidationError:
            return await save("NG_RECONCILIATION_REQUIRED")
        if completed.actual_qty != 1 or completed.location != snapshot["completion_location"]:
            return await save("NG_RECONCILIATION_REQUIRED")
        return await save(
            "NG_COMPLETED", state="NG_COMPLETED", pending=False, completed_at=self.clock()
        )

    def _output_snapshot(self, line, output):
        if (
            line is None
            or output is None
            or output.work_line_id != line.id
            or list(line.device_bindings.values()).count(output.device_code) != 1
            or not (location := self.direct_put_contracts.output_locations.get(line.line_code))
        ):
            return None
        return {
            **self._device_snapshot(output, line.version, "OUTPUT_ARM"),
            "completion_location": location,
        }

    async def _allocate(self, session, row, action, line, output, save, **values):
        snapshot = self._output_snapshot(line, output)
        if snapshot is None:
            return await save("OUTPUT_ARM_CONFIGURATION_REQUIRED", **values)
        if action["kind"] == "ALLOCATE_STORAGE" and action["resource_snapshot"] != snapshot:
            return await save("ORIGINAL_OUTPUT_ARM_CHANGED", **values)
        position = line.position_bindings.get("OUTPUT_RACK")
        if not position or (row["rack_position_code"] and row["rack_position_code"] != position):
            return await save("ORIGINAL_RACK_POSITION_CHANGED", **values)
        latest = await self._latest_preparation(session, row["work_line_id"], position)
        if latest and latest["state"] not in ("READY", "DEPART_ACCEPTED"):
            return await save(
                "AWAITING_RACK", state="AWAITING_RACK", rack_preparation_id=latest["id"], **values
            )
        input_preparation = values.pop("input_preparation_id", None)
        if input_preparation is None and action["kind"] == "ALLOCATE_STORAGE":
            input_preparation = action.get("input_preparation_id")
        try:
            model = self.direct_put_contracts.allocate(
                workline_code=row["workline_code"], binding_id=row["binding_id"]
            )
        except ValidationError:
            return await save("ALLOCATION_RECONCILIATION_REQUIRED", **values)
        action_id = await self._send_request(
            session,
            row,
            action,
            "ALLOCATE_STORAGE",
            ALLOCATE,
            model,
            rack_generation_no=latest["generation_no"] if latest else 0,
            input_preparation_id=input_preparation,
            resource_snapshot=snapshot,
            round_no=action["round_no"] + 1 if action["kind"] == "ALLOCATE_STORAGE" else 1,
        )
        return await save(
            "AWAITING_ALLOCATION_RESULT",
            state="AWAITING_ALLOCATION",
            current_action_id=action_id,
            rack_position_code=position,
            rack_binding_version=line.version,
            next_check_at=None,
            **values,
        )

    async def _advance_allocation(self, session, row, action, line, output, issue, save):
        if action["operation"] != ALLOCATE or action["request_data"] != {
            "workline_code": row["workline_code"],
            "binding_id": row["binding_id"],
        }:
            return await save("ALLOCATION_RECONCILIATION_REQUIRED")
        if row["next_check_at"] is not None and self.preparation_table is not None:
            latest = await self._latest_preparation(
                session, row["work_line_id"], row["rack_position_code"]
            )
            if latest and latest["generation_no"] > action["rack_generation_no"]:
                await self._associate_preparation(session, action, latest)
                return await save(
                    "AWAITING_RACK",
                    state="AWAITING_RACK",
                    rack_preparation_id=latest["id"],
                    next_check_at=None,
                )
        if row["next_check_at"] is not None:
            # SQLite returns naive UTC timestamps from DateTime(timezone=True).
            due = row["next_check_at"].replace(tzinfo=UTC)
            if self.clock() < due:
                return "NO_AVAILABLE_RACK"
            if (
                not action["evidence_history"]
                or action["evidence_history"][-1]["data"].get("result") != "WAIT"
            ):
                return await save("ALLOCATION_RECONCILIATION_REQUIRED")
            if issue:
                return await save(issue)
            return await self._allocate(session, row, action, line, output, save)
        result = await self._read_request(session, action)
        if not self._valid_response(result, "DECIDED"):
            if result.availability == "NOT_FOUND" and action["evidence_history"]:
                evidence = action["evidence_history"][-1]
            else:
                return await save(self._request_wait_reason(result, "ALLOCATION"))
        else:
            evidence = self._wms_evidence(result)
        try:
            decision = allocation_decision.validate_python(evidence["data"])
        except ValidationError:
            return await save("ALLOCATION_RECONCILIATION_REQUIRED")
        await self._consume(session, action, evidence)
        if decision.workline_code != row["workline_code"]:
            return await save("ALLOCATION_WORKLINE_MISMATCH")
        if self.preparation_table is not None:
            latest = await self._latest_preparation(
                session, row["work_line_id"], row["rack_position_code"]
            )
            if latest and latest["generation_no"] > action["rack_generation_no"]:
                await self._associate_preparation(session, action, latest)
                return await save(
                    "AWAITING_RACK",
                    state="AWAITING_RACK",
                    rack_preparation_id=latest["id"],
                    next_check_at=None,
                )
            if action.get("input_preparation_id"):
                basis = (
                    (
                        await session.execute(
                            select(self.preparation_table).where(
                                self.preparation_table.c.id == action["input_preparation_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                rack = getattr(decision, "rack_id", None) or getattr(
                    getattr(decision, "current_rack", None), "rack_id", None
                )
                if (
                    basis["state"] == "READY"
                    and (
                        (rack and rack != basis["incoming_rack_id"])
                        or isinstance(decision, Replenish)
                    )
                ) or (basis["state"] == "DEPART_ACCEPTED" and rack == basis["old_rack_id"]):
                    return await save("RACK_STATE_CONFLICT")
            if isinstance(decision, (Replenish, Replace)):
                if issue:
                    return await save(issue)
                if (
                    row["rack_position_code"] != line.position_bindings.get("OUTPUT_RACK")
                    or row["rack_binding_version"] != line.version
                ):
                    return await save("ORIGINAL_RACK_POSITION_CHANGED")
                if self._output_snapshot(line, output) != action["resource_snapshot"]:
                    return await save("ORIGINAL_OUTPUT_ARM_CHANGED")
                return await self._create_preparation(session, row, action, decision, latest, save)
        if isinstance(decision, Wait):
            return await save("NO_AVAILABLE_RACK", next_check_at=self.clock() + WAIT_INTERVAL)
        if not isinstance(decision, Allocated):
            return await save("RACK_PREPARATION_STAGE_REQUIRED")
        if issue:
            return await save(issue)
        snapshot = self._output_snapshot(line, output)
        if snapshot is None:
            return await save("OUTPUT_ARM_CONFIGURATION_REQUIRED")
        if snapshot != action["resource_snapshot"]:
            return await save("ORIGINAL_OUTPUT_ARM_CHANGED")
        if (
            row["rack_position_code"] != line.position_bindings.get("OUTPUT_RACK")
            or row["rack_binding_version"] != line.version
        ):
            return await save("ORIGINAL_RACK_POSITION_CHANGED")

        try:
            model = PutToBin(
                barcode=row["payload"].get("barcode"),
                bin_location=decision.bin_location,
                bin_cell_location=decision.cell_id[-1],
                bin_type=decision.bin_type,
                reel_totalthickness=decision.reel_totalthickness,
            )
        except ValidationError:
            return await save("PUT_RECONCILIATION_REQUIRED")
        action_id = str(uuid7())
        command = await self.capabilities.ecs.create(
            session,
            owner=OWNER,
            call_key=action_id,
            device_code=output.device_code,
            timeout_ms=30000,
            command=model,
        )
        await self._insert_action(
            session,
            row,
            "PUT_TO_BIN",
            model.model_dump(),
            predecessor=action,
            action_id=action_id,
            command_owner=OWNER.domain,
            call_key=action_id,
            command_code=command.command_code,
            device_code=output.device_code,
            resource_snapshot=snapshot,
        )
        return await save(
            "AWAITING_PUT_RESULT",
            state="AWAITING_PUT",
            current_action_id=action_id,
            allocation_action_id=action["id"],
        )

    async def _advance_put(self, session, row, action, actions, save):
        allocation = actions.get(row["allocation_action_id"])
        if (
            not action
            or action["kind"] != "PUT_TO_BIN"
            or action["command_owner"] != OWNER.domain
            or not allocation
            or allocation["kind"] != "ALLOCATE_STORAGE"
            or action["predecessor_action_id"] != allocation["id"]
            or not allocation["evidence_history"]
        ):
            return await save("PUT_RECONCILIATION_REQUIRED")
        command = await self.capabilities.ecs.get_command(
            session, owner=OWNER, command_code=action["command_code"]
        )
        if (
            command is None
            or command.task_type != "PUT_TO_BIN"
            or command.device_code != action["device_code"]
        ):
            return await save("PUT_RECONCILIATION_REQUIRED")
        result = command.result
        if result is None:
            reason = (
                "AWAITING_PUT_RESULT"
                if not command.needs_reconciliation
                and command.status in {"QUEUED", "DISPATCHING", "WAITING_RESULT"}
                else "PUT_RECONCILIATION_REQUIRED"
            )
            return await save(reason)
        await self._consume(session, action, self._ecs_evidence(asdict(command)))
        if (
            command.status != "SUCCEEDED"
            or command.needs_reconciliation
            or result["result"] != "SUCCESS"
            or result["command_code"] != action["command_code"]
            or result["device_code"] != action["device_code"]
        ):
            return await save("PUT_RECONCILIATION_REQUIRED")
        try:
            placed = MoveToLocationResult.model_validate(result["data"])
            decision = Allocated.model_validate(allocation["evidence_history"][-1]["data"])
            original = PutToBin.model_validate(action["request_data"])
            if (
                placed.actual_qty != 1
                or not placed.location
                or placed.location != action["resource_snapshot"].get("completion_location")
                or decision.workline_code != row["workline_code"]
                or decision.bin_location != original.bin_location
                or decision.cell_id[-1] != original.bin_cell_location
                or decision.bin_type != original.bin_type
                or decision.reel_totalthickness != original.reel_totalthickness
            ):
                return await save("PUT_RECONCILIATION_REQUIRED")
            model = self.direct_put_contracts.report(
                workline_code=row["workline_code"],
                binding_id=row["binding_id"],
                bin_code=decision.bin_code,
                bin_location=decision.bin_location,
                cell_id=decision.cell_id,
                completed_at=result["finish_time"],
            )
        except ValidationError:
            return await save("PUT_RECONCILIATION_REQUIRED")
        action_id = await self._send_request(
            session, row, action, "REPORT_COMPLETION", REPORT, model
        )
        return await save(
            "AWAITING_REPORT_RESULT",
            state="AWAITING_REPORT",
            current_action_id=action_id,
            placed_at_ms=model.completed_at,
        )

    async def _advance_report(self, session, action, save):
        if not action or action["kind"] != "REPORT_COMPLETION" or action["operation"] != REPORT:
            return await save("REPORT_RECONCILIATION_REQUIRED")
        result = await self._read_request(session, action)
        if not self._valid_response(result, "RECORDED", "DUPLICATE") or result.response_data != {}:
            return await save(self._request_wait_reason(result, "REPORT"))
        await self._consume(session, action, self._wms_evidence(result))
        return await save("COMPLETED", state="COMPLETED", pending=False, completed_at=self.clock())

    async def _lock_point(self, session, work_line_id):
        jobs = await self.rack_dispatch.admission_jobs(session, work_line_id=work_line_id)
        ids = tuple(job.id for job in jobs)
        await self.rack_dispatch.lock_jobs(session, job_ids=ids)
        table = self.preparation_table
        await session.execute(
            select(table)
            .where(table.c.work_line_id == work_line_id)
            .order_by(table.c.id)
            .with_for_update()
        )

    async def _latest_preparation(self, session, line_id, point):
        if self.preparation_table is None:
            return None
        table = self.preparation_table
        return (
            (
                await session.execute(
                    select(table)
                    .where(table.c.work_line_id == line_id, table.c.rack_position_code == point)
                    .order_by(table.c.generation_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _associate_preparation(self, session, action, preparation):
        if action["kind"] == "ALLOCATE_STORAGE" and action.get("result_preparation_id") is None:
            await session.execute(
                self.action_table.update()
                .where(self.action_table.c.id == action["id"])
                .values(result_preparation_id=preparation["id"])
            )

    async def _create_preparation(self, session, row, action, decision, latest, save):
        old = getattr(getattr(decision, "current_rack", None), "rack_id", None)
        incoming = decision.incoming_rack
        if incoming.rack_id == old:
            return await save("PREPARATION_PLAN_CONFLICT")
        # Read the whole point's responsibility set in the same serializable snapshot.
        jobs, actions = self.scan_table, self.action_table
        predecessors = list(
            await session.scalars(
                select(actions.c.id)
                .join(jobs, actions.c.job_id == jobs.c.id)
                .where(
                    jobs.c.work_line_id == row["work_line_id"],
                    jobs.c.rack_position_code == row["rack_position_code"],
                    actions.c.kind == "PUT_TO_BIN",
                    jobs.c.placed_at_ms.is_(None),
                )
                .order_by(actions.c.id)
            )
        )
        identity = str(uuid7())
        await session.execute(
            self.preparation_table.insert().values(
                id=identity,
                work_line_id=row["work_line_id"],
                workline_code=row["workline_code"],
                rack_position_code=row["rack_position_code"],
                rack_binding_version=row["rack_binding_version"],
                generation_no=(latest["generation_no"] if latest else 0) + 1,
                mode="REPLACE" if old else "REPLENISH",
                old_rack_id=old,
                incoming_rack_id=incoming.rack_id,
                incoming_target_face=incoming.target_face,
                source_allocation_action_id=action["id"],
                predecessor_action_ids=predecessors,
                state="AWAITING_DEPARTURE" if old else "MOVING_IN",
                created_at=self.clock(),
                updated_at=self.clock(),
            )
        )
        await self._associate_preparation(session, action, {"id": identity})
        return await save(
            "AWAITING_RACK", state="AWAITING_RACK", rack_preparation_id=identity, next_check_at=None
        )

    async def _resume_rack(self, session, row, action, line, output, save):
        latest = await self._latest_preparation(
            session, row["work_line_id"], row["rack_position_code"]
        )
        if not latest or not row["rack_preparation_id"]:
            return await save("PREPARATION_RECONCILIATION_REQUIRED")
        if latest["state"] not in ("READY", "DEPART_ACCEPTED"):
            return await save("AWAITING_RACK", rack_preparation_id=latest["id"])
        return await self._allocate(
            session,
            row,
            action,
            line,
            output,
            save,
            input_preparation_id=latest["id"],
            rack_preparation_id=latest["id"],
        )

    async def advance_preparation(self, preparation_id: str) -> str:
        if self.preparation_table is None:
            return "RACK_PREPARATION_STAGE_REQUIRED"
        for attempt in range(3):
            try:
                async with self.capabilities.session_factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    return await self._advance_preparation(session, preparation_id)
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def _advance_preparation(self, session, preparation_id):
        from app.businesses._components.rack_dispatch.contracts import (
            Coverage,
            DirectHandoff,
            FactRef,
            JobOrigin,
            PointDemand,
            PointRef,
        )
        from app.integrations.wms_transport import TransportCaller

        table, actions, jobs = self.preparation_table, self.action_table, self.scan_table
        query = select(table).where(table.c.id == preparation_id)
        located = (await session.execute(query)).mappings().one_or_none()
        if located is None:
            return "NOT_FOUND"
        line = await session.scalar(
            select(WorkLine).where(WorkLine.id == located["work_line_id"]).with_for_update()
        )
        devices = list(
            await session.scalars(
                select(Device)
                .where(Device.work_line_id == located["work_line_id"])
                .order_by(Device.id)
                .with_for_update()
            )
        )
        await self._lock_point(session, located["work_line_id"])
        prep = (await session.execute(query.with_for_update())).mappings().one()
        host_rows = (
            (
                await session.execute(
                    select(jobs)
                    .where(jobs.c.work_line_id == prep["work_line_id"])
                    .order_by(jobs.c.id)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        local = list(
            (
                await session.execute(
                    select(actions)
                    .where(
                        or_(
                            actions.c.preparation_id == preparation_id,
                            actions.c.id == prep["source_allocation_action_id"],
                            actions.c.id.in_(prep["predecessor_action_ids"]),
                        )
                    )
                    .order_by(actions.c.id)
                    .with_for_update()
                )
            ).mappings()
        )

        async def save(reason, **values):
            await session.execute(
                table.update()
                .where(table.c.id == preparation_id)
                .values(wait_reason=reason, updated_at=self.clock(), **values)
            )
            return reason

        if prep["state"] in ("READY", "DEPART_ACCEPTED"):
            return prep["state"]
        current = next((a for a in local if a["id"] == prep["current_action_id"]), None)
        if prep["current_action_id"] is not None and current is None:
            return await save("PREPARATION_RECONCILIATION_REQUIRED")
        if current is None and (
            prep["state"] == "MOVING_OUT"
            or any(
                a["preparation_id"] == prep["id"]
                and (prep["state"] == "AWAITING_DEPARTURE" or a["kind"] == "MOVE_RACK_IN")
                for a in local
            )
        ):
            return await save("PREPARATION_RECONCILIATION_REQUIRED")
        issue = (
            not line
            or not line.is_enabled
            or line.business_code != "coarse_sorting"
            or line.line_code != prep["workline_code"]
            or line.version != prep["rack_binding_version"]
            or line.position_bindings.get("OUTPUT_RACK") != prep["rack_position_code"]
            or not devices
        )
        # Re-read the point predicates, even when the fixed predecessor set is empty.
        latest = await self._latest_preparation(
            session, prep["work_line_id"], prep["rack_position_code"]
        )
        point_puts = list(
            (
                await session.execute(
                    select(actions)
                    .join(jobs, actions.c.job_id == jobs.c.id)
                    .where(
                        jobs.c.work_line_id == prep["work_line_id"],
                        jobs.c.rack_position_code == prep["rack_position_code"],
                        actions.c.kind == "PUT_TO_BIN",
                        jobs.c.placed_at_ms.is_(None),
                    )
                )
            ).mappings()
        )
        scoped_jobs = {
            row["id"]
            for row in host_rows
            if row["rack_position_code"] == prep["rack_position_code"]
        }
        original_puts = {
            a["id"] for a in local if a["kind"] == "PUT_TO_BIN" and a["job_id"] in scoped_jobs
        }
        clear = set(prep["predecessor_action_ids"]).issubset(original_puts)
        for put in {
            a["id"]: a for a in [*point_puts, *local] if a["kind"] == "PUT_TO_BIN"
        }.values():
            command = await self.capabilities.ecs.get_command(
                session, owner=OWNER, command_code=put["command_code"]
            )
            try:
                placed = MoveToLocationResult.model_validate(command.result["data"])
                good = (
                    command.status == "SUCCEEDED"
                    and not command.needs_reconciliation
                    and command.device_code == put["device_code"]
                    and command.result["result"] == "SUCCESS"
                    and placed.actual_qty == 1
                    and placed.location == put["resource_snapshot"]["completion_location"]
                )
            except (AttributeError, KeyError, TypeError, ValidationError):
                good = False
            clear = clear and good
        allow = not issue and clear and latest and latest["id"] == prep["id"]
        source = next((a for a in local if a["id"] == prep["source_allocation_action_id"]), None)
        if (
            not source
            or source["kind"] != "ALLOCATE_STORAGE"
            or source["result_preparation_id"] != prep["id"]
        ):
            return await save("PREPARATION_RECONCILIATION_REQUIRED")
        output = next(
            (
                d
                for d in devices
                if line and d.device_code == line.device_bindings.get("OUTPUT_ARM")
            ),
            None,
        )
        issue = issue or self._output_snapshot(line, output) != source["resource_snapshot"]
        allow = bool(allow and not issue)
        try:
            plan = allocation_decision.validate_python(source["evidence_history"][-1]["data"])
            valid_plan = (
                isinstance(plan, (Replenish, Replace))
                and plan.workline_code == prep["workline_code"]
                and plan.incoming_rack.rack_id == prep["incoming_rack_id"]
                and plan.incoming_rack.target_face == prep["incoming_target_face"]
                and getattr(getattr(plan, "current_rack", None), "rack_id", None)
                == prep["old_rack_id"]
            )
        except (ValidationError, KeyError, IndexError, TypeError):
            valid_plan = False
        if not valid_plan:
            return await save("PREPARATION_PLAN_CONFLICT")
        dispatch = self.rack_dispatch
        owned = await dispatch.jobs(
            session,
            work_line_id=prep["work_line_id"],
            source_kinds=("COARSE_INCOMING", "COARSE_WMS_PRESENT"),
        )
        entering = prep["state"] == "MOVING_IN"
        rack_id = prep["incoming_rack_id"] if entering else prep["old_rack_id"]
        public = (
            await dispatch.progress(session, job_id=current["rack_job_id"]) if current else None
        )
        if public and (
            public.origin.work_line_id != prep["work_line_id"]
            or public.origin.workline_code != prep["workline_code"]
            or public.origin.point != prep["rack_position_code"]
            or public.origin.rack_id != rack_id
        ):
            return await save("RACK_RECONCILIATION_REQUIRED")
        if public is None and current:
            return await save("RACK_RECONCILIATION_REQUIRED")
        if public is None:
            if not allow:
                return await save(
                    "ORIGINAL_RACK_CONFIGURATION_CHANGED" if issue else "AWAITING_PREDECESSORS"
                )
            public = next(
                (
                    j
                    for j in owned
                    if j.pending
                    and j.origin.rack_id == rack_id
                    and j.origin.point == prep["rack_position_code"]
                ),
                None,
            )
            if public is None:
                proof = {
                    "source_ref": source["id"],
                    "point": prep["rack_position_code"],
                    "operation": source["operation"],
                    "operation_id": source["operation_id"],
                    "message_id": source["message_id"],
                    "decision": source["evidence_history"][-1]["data"],
                }
                identity = prep["id"] if entering else str(uuid7())
                origin = JobOrigin(
                    "COARSE_INCOMING" if entering else "COARSE_WMS_PRESENT",
                    prep["id"] if entering else source["id"],
                    prep["work_line_id"],
                    rack_id,
                    None,
                    prep["workline_code"],
                    prep["rack_position_code"],
                    prep["incoming_target_face"] if entering else None,
                    TransportCaller(prep["workline_code"], prep["rack_position_code"]),
                    identity,
                    {
                        "positions": {"OUTPUT_RACK": prep["rack_position_code"]},
                        **({"wms_presence": proof} if not entering else {}),
                    },
                )
                await dispatch.register(session, origin)
                public = await dispatch.progress(session, job_id=identity)
            if entering:
                action_id = await dispatch.prepare(
                    session,
                    job_id=public.id,
                    kind="ENTER",
                    source_ref=prep["id"],
                    face=prep["incoming_target_face"],
                )
                if not action_id:
                    return await save("RACK_ENTRY_BLOCKED")
                public = await dispatch.progress(session, job_id=public.id)
        point = PointRef(
            "coarse_sorting", prep["work_line_id"], public.id, rack_id, prep["rack_position_code"]
        )
        coverage = Coverage(
            "coarse_preparation",
            (
                "coarse_sorting",
                str(prep["work_line_id"]),
                public.id,
                rack_id,
                prep["rack_position_code"],
            ),
            FactRef("preparation", (prep["id"],), (str(prep["generation_no"]),)),
            tuple(
                FactRef("put", (a["id"],), ("CLEAR" if clear else "UNKNOWN",)) for a in point_puts
            ),
        )
        public = await dispatch.advance(
            session,
            job_id=public.id,
            demand=PointDemand(point, "CONTINUE" if entering else "NONE", (coverage,)),
            handoff=DirectHandoff(point, "CLEAR" if clear else "BLOCKED", (coverage,)),
            now_ms=int(self.clock().timestamp() * 1000),
            allow_new_departure=bool(allow and not entering),
        )
        current = await self._mirror_rack_actions(session, prep, public, local)
        if not current:
            return await save(public.review_reason or "AWAITING_PREDECESSORS")
        if entering:
            original = next(a for a in public.actions if a.id == current["rack_action_id"])
            result = await dispatch.final_result(session, job_id=public.id, action_id=original.id)
            if result:
                await self._consume(
                    session,
                    current,
                    {
                        "transport_task_id": result.transport_task_id,
                        "outcome_revision": result.outcome_revision,
                        "outcome": result.outcome,
                        "arrival_face_reported": result.arrival_face_reported,
                    },
                )
            if (
                result
                and clear
                and result.availability == "FOUND"
                and result.closure == "CLOSED"
                and not result.has_pending_outcome
                and result.arrival_face_reported
                and original.phase == "APPLIED"
                and result.outcome
                and result.outcome["status"] == "SUCCEEDED"
                and result.outcome["rack_id"] == rack_id
                and result.outcome["final_position"]
                == {"kind": "RACK_POSITION", "location_code": prep["rack_position_code"]}
                and result.outcome["arrival_face"] == prep["incoming_target_face"]
            ):
                return await save(
                    "READY", state="READY", current_action_id=current["id"], ready_at=self.clock()
                )
            return await save(
                "RACK_RECONCILIATION_REQUIRED"
                if result and result.outcome
                else "AWAITING_RACK_RESULT",
                current_action_id=current["id"],
            )
        if public.closure_reason == "DEPARTURE_ACK" and not public.pending:
            if not allow:
                return await save(
                    "ORIGINAL_RACK_CONFIGURATION_CHANGED" if issue else "AWAITING_PREDECESSORS",
                    state="MOVING_OUT",
                    current_action_id=current["id"],
                )
            # Keep the accepted old job separate from the incoming rack's original cycle.
            await session.execute(
                table.update()
                .where(table.c.id == prep["id"])
                .values(state="MOVING_IN", current_action_id=None)
            )
            reason = await self._advance_preparation(session, preparation_id)
            fresh = (await session.execute(query)).mappings().one()
            if fresh["current_action_id"] is None:
                await session.execute(
                    table.update()
                    .where(table.c.id == prep["id"])
                    .values(state="MOVING_OUT", current_action_id=current["id"])
                )
            return reason
        kind = current["kind"]
        return await save(
            public.review_reason
            or (
                "AWAITING_DEPARTURE_RESULT"
                if kind == "DEPARTURE_DECIDE"
                else "AWAITING_RACK_ACCEPTANCE"
            ),
            state="AWAITING_DEPARTURE" if kind == "DEPARTURE_DECIDE" else "MOVING_OUT",
            current_action_id=current["id"],
        )

    async def _mirror_rack_actions(self, session, prep, public, local):
        owned = [a for a in local if a["preparation_id"] == prep["id"]]
        predecessor = next((a for a in owned if a["id"] == prep["current_action_id"]), None)
        if predecessor is None and prep["state"] == "MOVING_IN":
            predecessor = next((a for a in owned if a["kind"] == "MOVE_RACK_OUT"), None)
        for original in public.actions:
            kind = {
                "ENTER": "MOVE_RACK_IN",
                "DEPART": "MOVE_RACK_OUT",
                "DEPARTURE_DECIDE": "DEPARTURE_DECIDE",
            }.get(original.kind)
            if kind is None or (original.kind == "ENTER" and prep["state"] != "MOVING_IN"):
                continue
            current = next((a for a in owned if a["rack_action_id"] == original.id), None)
            if current is None:
                refs = (
                    dict(
                        operation=original.operation,
                        operation_id=original.decision_operation_id,
                        message_id=original.message_id,
                    )
                    if original.kind == "DEPARTURE_DECIDE"
                    else dict(
                        transport_task_id=original.transport_task_id,
                        client_request_id=original.client_request_id,
                        cycle_id=public.origin.cycle_id,
                        transport_caller=asdict(original.caller),
                        resource_snapshot={
                            "rack_id": public.origin.rack_id,
                            "template": original.template,
                            "source": original.source,
                            "target": original.target,
                            "target_face": original.target_face,
                        },
                    )
                )
                current = dict(
                    id=str(uuid7()),
                    job_id=None,
                    preparation_id=prep["id"],
                    kind=kind,
                    round_no=1 + sum(a["kind"] == kind for a in owned),
                    predecessor_action_id=predecessor["id"] if predecessor else None,
                    rack_job_id=public.id,
                    rack_action_id=original.id,
                    request_data=original.request
                    or {
                        "source": original.source,
                        "target": original.target,
                        "target_face": original.target_face,
                    },
                    evidence_history=[],
                    created_at=self.clock(),
                    **refs,
                )
                await session.execute(self.action_table.insert().values(**current))
                owned.append(current)
            if original.decision:
                await self._consume(
                    session,
                    current,
                    {"operation_id": original.decision_operation_id, "data": original.decision},
                )
            if original.acceptance_basis:
                await self._consume(
                    session, current, {"acceptance_basis": original.acceptance_basis}
                )
            predecessor = current
        return predecessor

    async def scan_pending(self, *, limit=100) -> int:
        """Bound this sweep to existing pending IDs; new work gets its own wakeup."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        table = self.scan_table
        pending = select(table.c.id).where(table.c.pending.is_(True))
        if self.ng_locations is None:
            pending = pending.where(
                table.c.state != "AWAITING_NG",
                table.c.wait_reason != "AWAITING_NG_STAGE",
                table.c.ng_reason_code.is_(None),
            )
        if not self.direct_put:
            pending = pending.where(
                table.c.wait_reason != "AWAITING_ALLOCATION_STAGE",
                table.c.state.not_in(DIRECT_PUT_STATES),
            )
        scans = [(table, pending, self.advance)]
        if self.preparation_table is not None:
            prep = self.preparation_table
            scans.append(
                (
                    prep,
                    select(prep.c.id).where(prep.c.state.not_in(("READY", "DEPART_ACCEPTED"))),
                    self.advance_preparation,
                )
            )
        cursors = []
        async with self.capabilities.session_factory() as session:
            for subject, query, advance in scans:
                upper = await session.scalar(query.order_by(subject.c.id.desc()).limit(1))
                if upper is not None:
                    cursors.append([subject, query, advance, "", upper])
        total = 0
        while cursors:
            for cursor in list(cursors):
                subject, query, advance, last_id, upper = cursor
                async with self.capabilities.session_factory() as session:
                    ids = list(
                        await session.scalars(
                            query.where(subject.c.id > last_id, subject.c.id <= upper)
                            .order_by(subject.c.id)
                            .limit(limit)
                        )
                    )
                if not ids:
                    cursors.remove(cursor)
                    continue
                for identity in ids:
                    try:
                        await advance(identity)
                    except Exception as error:
                        log_exception(
                            logger,
                            "Coarse sorting advance failed",
                            error,
                            component="coarse_sorting",
                            phase="advance",
                            context={"request_id": identity},
                        )
                    total += 1
                cursor[3] = ids[-1]
        return total


async def read_scan(session, digest: str) -> dict | None:
    """Read the persisted scan in the caller's transaction without advancing it."""
    from .models import CoarseSortingScan

    table = CoarseSortingScan.__table__
    row = (
        (await session.execute(select(table).where(table.c.event_digest == digest)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "state",
            "next_check_at",
            "rack_position_code",
            "rack_binding_version",
            "current_action_id",
            "allocation_action_id",
            "rack_preparation_id",
            "placed_at_ms",
            "updated_at",
            "completed_at",
            "ng_reason_code",
        }
    }
