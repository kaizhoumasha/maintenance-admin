"""Read-only original-message results; no delivery or business-completion side effects."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.strict_json import json_object
from app.integrations.wms_envelope import (
    outgoing_message,
    response_projection,
    response_row,
    stored_response,
)
from app.models import ReliableMessage

from .adapter import ADAPTERS, PROTOCOL_OF
from .operation import lookup


@dataclass(frozen=True, slots=True, kw_only=True)
class WmsRequestResult:
    message_id: int
    operation: str
    operation_id: str
    availability: str
    target: str | None = None
    delivery_status: str | None = None
    delivery_terminal: bool | None = None
    delivery_uncertain: bool | None = None
    fail_reason: str | None = None
    response_state: str = "UNAVAILABLE"
    http_status: int | None = None
    response_code: str | None = None
    response_timestamp: int | None = None
    response_data: dict | None = None


async def get_result(
    session: AsyncSession, *, message_id: int, operation: str, operation_id: str
) -> WmsRequestResult:
    identity = dict(message_id=message_id, operation=operation, operation_id=operation_id)
    clause, columns = response_projection(ReliableMessage.__table__)
    with session.no_autoflush:
        row = (
            (
                await session.execute(
                    select(*columns.values())
                    .select_from(clause)
                    .where(ReliableMessage.id == message_id)
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        return WmsRequestResult(**identity, availability="NOT_FOUND")
    if row["operation"] != operation or row["operation_id"] != operation_id:
        return WmsRequestResult(**identity, availability="IDENTITY_MISMATCH")
    row = response_row(row)
    raw = json_object(row["request_raw"]) if row["request_raw"] is not None else None
    if not raw or raw.get("operation") != operation or raw.get("operation_id") != operation_id:
        return WmsRequestResult(**identity, availability="INVALID")
    registered = lookup(operation)
    adapter = ADAPTERS[PROTOCOL_OF[registered.endpoint]] if registered else None
    return WmsRequestResult(
        **identity,
        availability="FOUND",
        target=row["target"],
        delivery_status=row["status"],
        delivery_terminal=row["status"] in {"DONE", "FAILED", "EXPIRED", "ABANDONED"},
        delivery_uncertain=row["delivery_uncertain"],
        fail_reason=row["fail_reason"],
        **stored_response(outgoing_message(row), row, adapter),
    )
