"""Read original EVENT evidence in the caller's transaction, without consuming it."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, select

from app.models import IntegrationExchange

from .envelope import EcsEventEnvelope


@dataclass(frozen=True)
class EventApplicationRef:
    subject_kind: str
    subject_id: str
    canonical_event_id: int
    predecessor_event_id: int | None = None
    separator_command_code: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.subject_kind, str)
            or not self.subject_kind
            or not isinstance(self.subject_id, str)
            or not self.subject_id
            or type(self.canonical_event_id) is not int
            or self.canonical_event_id <= 0
        ):
            raise ValueError("invalid event application reference")
        if self.predecessor_event_id is not None and (
            type(self.predecessor_event_id) is not int or self.predecessor_event_id <= 0
        ):
            raise ValueError("invalid predecessor event")


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    digest: str
    received_at: datetime
    envelope: dict
    context: dict
    application_ref: EventApplicationRef | None


@dataclass(frozen=True)
class EventReadResult:
    availability: Literal["FOUND", "NOT_FOUND", "UNAVAILABLE", "INVALID"]
    record: EventRecord | None = None


def _read(row):
    if row is None or row.operation != "ecs.event":
        return EventReadResult("NOT_FOUND")
    if row.event_envelope is None or row.event_context is None:
        return EventReadResult("UNAVAILABLE")
    try:
        envelope = EcsEventEnvelope.model_validate(row.event_envelope)
        context = row.event_context
        if (
            not isinstance(context, dict)
            or not {
                "owner_domain",
                "work_line_id",
                "device_id",
                "device_role",
                "device_version",
                "work_line_version",
                "component_key",
            }
            <= context.keys()
        ):
            raise ValueError("invalid event context")
        if (
            any(
                type(context[key]) is not int or context[key] < 0
                for key in (
                    "work_line_id",
                    "device_id",
                    "device_version",
                    "work_line_version",
                )
            )
            or not isinstance(context["device_role"], str)
            or not context["device_role"]
            or any(
                context[key] is not None and not isinstance(context[key], str)
                for key in ("owner_domain", "component_key")
            )
        ):
            raise ValueError("invalid event context")
        reference = EventApplicationRef(**row.application_ref) if row.application_ref else None
        return EventReadResult(
            "FOUND",
            EventRecord(
                row.receipt_id,
                row.operation_id,
                row.created_at,
                deepcopy(
                    envelope.model_dump(include={"device_code", "event_type", "timestamp", "data"})
                ),
                deepcopy(context),
                reference,
            ),
        )
    except (ValueError, TypeError):
        return EventReadResult("INVALID")


async def get_event(session, *, event_id: int) -> EventReadResult:
    with session.no_autoflush:
        row = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "RECEIPT",
                IntegrationExchange.receipt_active.is_(True),
                IntegrationExchange.receipt_id == event_id,
            )
        )
        return _read(row)


async def list_point_events(
    session,
    *,
    owner_domain: str,
    work_line_id: int,
    point: str,
    before_event_id: int | None = None,
    limit: int = 100,
) -> tuple[EventRecord, ...]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    table = IntegrationExchange
    query = select(table).where(
        table.role == "RECEIPT",
        table.receipt_active.is_(True),
        table.operation == "ecs.event",
        table.event_context["owner_domain"].as_string() == owner_domain,
        table.event_context["work_line_id"].as_integer() == work_line_id,
        table.event_context["device_role"].as_string() == point,
        or_(
            table.application_ref.is_(None),
            table.application_ref["canonical_event_id"].as_integer() == table.receipt_id,
        ),
    )
    if before_event_id is not None:
        query = query.where(table.receipt_id < before_event_id)
    with session.no_autoflush:
        rows = await session.scalars(query.order_by(table.receipt_id.desc()).limit(limit))
        return tuple(
            result.record for row in rows if (result := _read(row)).availability == "FOUND"
        )
