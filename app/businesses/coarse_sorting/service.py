"""Accept scan facts and advance their original validation and conveyor command."""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import Table, or_, select
from sqlalchemy.exc import DBAPIError

from app.businesses.runtime import Capabilities, require_runtime
from app.core.identifiers import uuid7
from app.diagnostics import log_exception
from app.integrations.ecs_callback.binding import DeviceBinding
from app.integrations.ecs_callback.registry import EcsEventContext
from app.integrations.ecs_command import CommandOwner
from app.integrations.wms_request import RequestPayload
from app.models import Device, WorkLine
from app.services.workline_service import business_configuration_issue, read_workline

from .contracts import ScanCompletedData, ValidateBindDecision
from .definition import DEFINITION
from .ecs_command import MoveForward

logger = logging.getLogger(__name__)
OWNER = CommandOwner("coarse_sorting")
VALIDATE = "inbound.coarse_sorting.validate_bind@v1"
NEXT_STAGES = ("AWAITING_NG_STAGE", "AWAITING_ALLOCATION_STAGE")


@dataclass(frozen=True)
class CoarseSortingService:
    scan_table: Table
    capabilities: Capabilities
    validate_bind: type[RequestPayload]

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
                payload=payload.model_dump(mode="json", exclude_unset=True),
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
                            execution_options={"isolation_level": "REPEATABLE READ"}
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
        devices = list(
            await session.scalars(
                select(Device)
                .where(
                    or_(
                        Device.id == located["device_id"],
                        Device.id == located["conveyor_device_id"],
                        Device.device_code == conveyor_code,
                    )
                )
                .order_by(Device.id)
                .with_for_update()
            )
        )
        row = (await session.execute(query.with_for_update())).mappings().one()

        async def save(reason, **values):
            await session.execute(
                table.update().where(table.c.id == scan_id).values(wait_reason=reason, **values)
            )
            return reason

        if not row["pending"] or row["wait_reason"] in NEXT_STAGES:
            return row["wait_reason"]
        device = next((d for d in devices if d.id == row["device_id"]), None)
        conveyor = next((d for d in devices if d.device_code == conveyor_code), None)
        if (
            line is None
            or device is None
            or conveyor is None
            or line.version != row["work_line_version"]
            or line.line_code != row["workline_code"]
            or device.version != row["device_version"]
            or device.device_code != row["device_code"]
            or device.work_line_id != line.id
            or conveyor.work_line_id != line.id
            or line.device_bindings.get("INPUT_ARM") != device.device_code
            or line.device_bindings.get("CONVEYOR") != conveyor.device_code
            or (
                row["conveyor_device_id"] is not None
                and (
                    conveyor.id != row["conveyor_device_id"]
                    or conveyor.version != row["conveyor_device_version"]
                )
            )
        ):
            return await save("ORIGINAL_BINDING_CHANGED")
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
        if binding.revision != row["binding_revision"]:
            return await save("ORIGINAL_BINDING_CHANGED")
        issue = await business_configuration_issue(
            session, binding, "coarse_sorting", definition_lookup=lambda _: DEFINITION
        )
        if issue:
            return await save(issue)
        if row["command_code"] is not None:
            command = await self.capabilities.ecs.get_command(
                session, owner=OWNER, command_code=row["command_code"]
            )
            if command is None:
                return await save("MOVE_RECONCILIATION_REQUIRED")
            result = command.result
            if result is not None:
                data = result.get("data", {})
                complete = (
                    command.status == "SUCCEEDED"
                    and not command.needs_reconciliation
                    and result.get("result") == "SUCCESS"
                    and type(data.get("actual_qty")) is int
                    and data["actual_qty"] == 1
                    and data.get("location") == "STATION_PIPELINE1_OUTPUT1"
                )
                return await save(
                    "AWAITING_ALLOCATION_STAGE" if complete else "MOVE_RECONCILIATION_REQUIRED",
                    move_result=asdict(command),
                )
            if command.needs_reconciliation or command.status in {
                "RECONCILING",
                "REJECTED",
                "EXPIRED",
                "FAILED",
            }:
                return await save("MOVE_RECONCILIATION_REQUIRED")
            return await save("AWAITING_MOVE_RESULT")
        if row["validation_operation_id"] is None:
            payload = row["payload"]
            operation_id = str(uuid7())
            reference = await self.capabilities.wms.send(
                session,
                self.validate_bind(
                    workline_code=row["workline_code"],
                    barcode=payload["barcode"],
                    reel_diameter=payload["reel_diameter"],
                    reel_thickness=payload["reel_thickness"],
                ),
                operation_id=operation_id,
            )
            return await save(
                "AWAITING_VALIDATION_RESULT",
                validation_operation_id=operation_id,
                validation_message_id=reference.message_id,
                conveyor_device_id=conveyor.id,
                conveyor_device_code=conveyor.device_code,
                conveyor_device_version=conveyor.version,
            )
        result = await self.capabilities.wms.get_result(
            session,
            message_id=row["validation_message_id"],
            operation=VALIDATE,
            operation_id=row["validation_operation_id"],
        )
        if not (
            result.availability == "FOUND"
            and result.response_state == "VALID"
            and result.http_status == 200
            and result.response_code == "DECIDED"
        ):
            reason = (
                "AWAITING_VALIDATION_RESULT"
                if result.availability == "FOUND"
                and not result.delivery_terminal
                and not result.delivery_uncertain
                else "VALIDATION_RECONCILIATION_REQUIRED"
            )
            return await save(reason)
        try:
            decision = ValidateBindDecision.model_validate(result.response_data)
        except ValidationError:
            return await save(
                "VALIDATION_RECONCILIATION_REQUIRED", validation_response=asdict(result)
            )
        if decision.result == "NG":
            return await save("AWAITING_NG_STAGE", validation_response=asdict(result))
        command = await self.capabilities.ecs.create(
            session,
            owner=OWNER,
            call_key=f"{scan_id}:MOVE_FORWARD",
            device_code=conveyor.device_code,
            timeout_ms=30000,
            command=MoveForward(barcode=row["payload"]["barcode"]),
        )
        return await save(
            "AWAITING_MOVE_RESULT",
            validation_response=asdict(result),
            binding_id=decision.binding_id,
            command_code=command.command_code,
        )

    async def scan_pending(self, *, limit=100) -> int:
        """Visit each current-stage pending identity once, including blocked earlier pages."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        table = self.scan_table
        last_id, total = "", 0
        while True:
            async with self.capabilities.session_factory() as session:
                ids = list(
                    await session.scalars(
                        select(table.c.id)
                        .where(
                            table.c.pending.is_(True),
                            table.c.id > last_id,
                            table.c.wait_reason.not_in(NEXT_STAGES),
                        )
                        .order_by(table.c.id)
                        .limit(limit)
                    )
                )
            if not ids:
                return total
            for scan_id in ids:
                try:
                    await self.advance(scan_id)
                except Exception as error:
                    log_exception(
                        logger,
                        "Coarse sorting advance failed",
                        error,
                        component="coarse_sorting",
                        phase="advance",
                        context={"request_id": scan_id},
                    )
                total += 1
            last_id = ids[-1]


async def read_scan(session, digest: str) -> dict | None:
    """Read the persisted scan in the caller's transaction without advancing it."""
    from .models import CoarseSortingScan

    table = CoarseSortingScan.__table__
    row = (
        (await session.execute(select(table).where(table.c.event_digest == digest)))
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None
