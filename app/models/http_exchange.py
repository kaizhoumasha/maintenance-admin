from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

Json = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class ReliableDeliveryAttempt(Base):
    __tablename__ = "reliable_delivery_attempt"
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_id: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    protocol: Mapped[str] = mapped_column(Text)
    exchange_id: Mapped[UUID] = mapped_column(Uuid)
    request_record_id: Mapped[UUID] = mapped_column(Uuid)
    response_record_id: Mapped[UUID | None] = mapped_column(Uuid)
    response_state: Mapped[str | None] = mapped_column(Text)
    request_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    response_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    response_http_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_verdict: Mapped[str | None] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str] = mapped_column(Text)
    business_key: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    peer_endpoint: Mapped[str] = mapped_column(Text)
    traffic_source: Mapped[str] = mapped_column(Text)
    http_method: Mapped[str] = mapped_column(Text)
    endpoint_path: Mapped[str] = mapped_column(Text)
    request_headers: Mapped[dict | None] = mapped_column(Json)
    request_query: Mapped[dict | None] = mapped_column(Json)
    response_headers: Mapped[dict | None] = mapped_column(Json)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)
    attempt_state: Mapped[str] = mapped_column(Text, default="OPEN")
    failure_kind: Mapped[str | None] = mapped_column(Text)
    late: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)


class HttpExchange:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    direction: Mapped[str] = mapped_column(Text)
    interaction_kind: Mapped[str] = mapped_column(Text)
    traffic_source: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    http_method: Mapped[str] = mapped_column(Text)
    endpoint_path: Mapped[str] = mapped_column(Text)
    peer_endpoint: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)
    request_headers: Mapped[dict | None] = mapped_column(Json)
    response_headers: Mapped[dict | None] = mapped_column(Json)
    request_query: Mapped[dict | None] = mapped_column(Json)
    request_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    response_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    request_payload: Mapped[object | None] = mapped_column(Json)
    response_payload: Mapped[object | None] = mapped_column(Json)
    request_wire_state: Mapped[str] = mapped_column(Text)
    response_wire_state: Mapped[str] = mapped_column(Text)
    request_wire_sha256: Mapped[str | None] = mapped_column(Text)
    response_wire_sha256: Mapped[str | None] = mapped_column(Text)
    request_wire_bytes: Mapped[int | None] = mapped_column(BigInteger)
    response_wire_bytes: Mapped[int | None] = mapped_column(BigInteger)
    attempt_state: Mapped[str] = mapped_column(Text)
    failure_kind: Mapped[str | None] = mapped_column(Text)
    close_reason: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    protocol_code: Mapped[str | None] = mapped_column(Text)
    protocol_verdict: Mapped[str | None] = mapped_column(Text)
    handling_outcome: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    reported_result: Mapped[str | None] = mapped_column(Text)
    replayed_from_id: Mapped[UUID | None] = mapped_column(Uuid)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    lease_id: Mapped[str | None] = mapped_column(Text)
    receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    late_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_revision: Mapped[int] = mapped_column(default=0)


def _constraints(name, kinds, *keys):
    return (
        CheckConstraint("direction IN ('INBOUND', 'OUTBOUND')", name="direction"),
        CheckConstraint("traffic_source IN ('PEER', 'MOCK')", name="traffic_source"),
        CheckConstraint(f"interaction_kind IN {kinds}", name="interaction_kind"),
        CheckConstraint("(message_id IS NULL) = (lease_id IS NULL)", name="lease_pair"),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status"),
        CheckConstraint("(attempt_state = 'OPEN') = (finished_at IS NULL)", name="finish"),
        *(
            CheckConstraint(
                f"(({side}_wire_state IN ('FULL', 'PREVIEW') AND {side}_wire IS NOT NULL "
                f"AND {side}_wire_sha256 IS NOT NULL AND {side}_wire_bytes IS NOT NULL "
                f"AND {side}_wire_bytes = length({side}_wire)) OR "
                f"({side}_wire_state IN ('UNAVAILABLE', 'PURGED') AND {side}_wire IS NULL)) "
                f"AND ({side}_wire_state = 'FULL' OR {side}_payload IS NULL)",
                name=f"{side}_body",
            )
            for side in ("request", "response")
        ),
        Index(f"{name}_time_idx", "started_at", "id"),
        Index(
            f"{name}_replay_idx",
            "replayed_from_id",
            postgresql_where=text("replayed_from_id IS NOT NULL"),
            sqlite_where=text("replayed_from_id IS NOT NULL"),
        ),
        Index(
            f"{name}_lease_idx",
            "message_id",
            "lease_id",
            unique=True,
            postgresql_where=text("lease_id IS NOT NULL"),
            sqlite_where=text("lease_id IS NOT NULL"),
        ),
        *(
            Index(
                f"{name}_{key}_idx",
                key,
                "started_at",
                "id",
                postgresql_where=text(f"{key} IS NOT NULL"),
                sqlite_where=text(f"{key} IS NOT NULL"),
            )
            for key in ("trace_id", *keys)
        ),
    )


class WmsHttpExchange(HttpExchange, Base):
    __tablename__ = "wms_http_exchange"
    __table_args__ = _constraints(__tablename__, "('DECISION', 'FACT', 'CALLBACK')", "operation_id")
    operation_id: Mapped[UUID | None] = mapped_column(Uuid)
    operation: Mapped[str | None] = mapped_column(Text)


class EcsHttpExchange(HttpExchange, Base):
    __tablename__ = "ecs_http_exchange"
    __table_args__ = _constraints(
        __tablename__, "('COMMAND', 'RESULT', 'EVENT', 'STATUS')", "command_code", "device_code"
    )
    command_code: Mapped[str | None] = mapped_column(Text)
    device_code: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str | None] = mapped_column(Text)


class TransportHttpExchange(HttpExchange, Base):
    __tablename__ = "transport_http_exchange"
    __table_args__ = (
        *_constraints(
            __tablename__, "('SUBMIT', 'POSITION_CALLBACK', 'RESULT_CALLBACK')", "transport_task_id"
        ),
        CheckConstraint(
            "task_match_state IN ('MATCHED', 'UNMATCHED', 'UNKNOWN')", name="task_match"
        ),
    )
    transport_task_id: Mapped[str | None] = mapped_column(Text)
    task_match_state: Mapped[str] = mapped_column(Text)
    outcome_revision: Mapped[int | None] = mapped_column(BigInteger)
