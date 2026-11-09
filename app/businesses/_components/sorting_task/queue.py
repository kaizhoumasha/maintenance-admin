"""Shared sorting task acceptance and queue rules."""

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.identifiers import uuid7
from app.integrations.wms_callback import Conflict
from app.reliable import ScopeRef

from .contracts import Issued, QueueChanged
from .request.sorting_task_prepare_v1 import Prepare


async def insert_issued(ctx, table, payload: Issued, *, work_line_id: int) -> bool:
    insert = pg_insert if ctx.session.get_bind().dialect.name == "postgresql" else sqlite_insert
    issued = payload.model_dump(mode="json")
    task_id = await ctx.session.scalar(
        insert(table)
        .values(
            **payload.model_dump(mode="json", exclude={"not_before"}),
            not_before_ms=payload.not_before,
            work_line_id=work_line_id,
            issued_operation_id=ctx.operation_id,
            issued_timestamp=ctx.timestamp,
            issued_data=issued,
        )
        .on_conflict_do_nothing(index_elements=[table.c.task_id])
        .returning(table.c.task_id)
    )
    return task_id is not None


def queue_changes(
    payload: QueueChanged,
    *,
    status: str,
    queue_revision: int,
    dispatch_sequence: int,
    not_before: int | None,
) -> dict[str, int] | Conflict:
    if status != "QUEUED":
        return Conflict({"reason_code": "STATE_CONFLICT"})
    if payload.queue_revision != queue_revision + 1:
        return Conflict({"reason_code": "REVISION_CONFLICT"})
    changes = payload.model_dump(include={"dispatch_sequence", "not_before"})
    current = {"dispatch_sequence": dispatch_sequence, "not_before": not_before}
    if all(current[key] == value for key, value in changes.items()):
        return Conflict({"reason_code": "STATE_CONFLICT"})
    return changes


async def prepare_next(session, table, *, line, owner_domain, now_ms, wms):
    """Under the host's line lock, select one candidate and enqueue its original prepare."""
    if line is None or not line.is_enabled or line.business_code != owner_domain:
        return None
    if await session.scalar(
        select(table.c.task_id)
        .where(
            table.c.work_line_id == line.id,
            table.c.status.in_(("PREPARING", "EXECUTING")),
        )
        .limit(1)
    ):
        return None
    task_id = await session.scalar(
        select(table.c.task_id)
        .where(
            table.c.work_line_id == line.id,
            table.c.owner_domain == owner_domain,
            table.c.workline_code == line.line_code,
            table.c.status == "QUEUED",
            or_(table.c.not_before_ms.is_(None), table.c.not_before_ms <= now_ms),
        )
        .order_by(table.c.dispatch_sequence, table.c.issued_timestamp, table.c.task_id)
        .limit(1)
        .with_for_update()
    )
    if task_id is None:
        return None
    operation_id = str(uuid7())
    payload = Prepare(task_id=task_id, workline_code=line.line_code)
    message = await wms.send(
        session, payload, operation_id=operation_id, scope=ScopeRef(owner_domain, "TASK", task_id)
    )
    await session.execute(
        table.update().where(table.c.task_id == task_id).values(status="PREPARING")
    )
    return payload, operation_id, message.message_id
