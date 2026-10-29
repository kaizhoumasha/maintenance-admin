"""Accept coarse scans in the receiver-owned transaction without starting external work."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Table, select

from app.businesses.runtime import require_runtime
from app.core.identifiers import uuid7
from app.integrations.ecs_callback.registry import EcsEventContext
from app.services.workline_service import business_configuration_issue, read_workline

from .contracts import ScanCompletedData


@dataclass(frozen=True)
class CoarseSortingService:
    scan_table: Table

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
