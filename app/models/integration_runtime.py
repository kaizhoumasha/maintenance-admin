"""Original protocol facts retained independently of HTTP troubleshooting history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, LargeBinary, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .integration_history import JSON_VALUE


class CallbackReceipt(Base):
    __tablename__ = "callback_receipt"
    __table_args__ = (
        Index(
            "callback_receipt_active_identity",
            "operation",
            "operation_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
        Index("callback_receipt_operation_id_idx", "operation_id"),
        Index("callback_receipt_expiry_idx", "created_at", "receipt_id"),
    )
    receipt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    operation: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(Text)
    owner_domain: Mapped[str | None] = mapped_column(Text)
    device_code: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean)
    raw_digest: Mapped[str] = mapped_column(Text)
    effective_digest: Mapped[str | None] = mapped_column(Text)
    compare_mode: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    resp_timestamp: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reply_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    reply_sha256: Mapped[str | None] = mapped_column(Text)
    reply_attempt_id: Mapped[str | None] = mapped_column(Text)
    event_envelope: Mapped[dict | None] = mapped_column(JSON_VALUE)
    event_context: Mapped[dict | None] = mapped_column(JSON_VALUE)
    application_ref: Mapped[dict | None] = mapped_column(JSON_VALUE)
    diagnostic: Mapped[list | None] = mapped_column(JSON_VALUE)


class EcsCommand(Base):
    __tablename__ = "ecs_command"
    __table_args__ = (
        Index("ecs_command_call_key", "owner_domain", "call_key_digest", unique=True),
    )
    command_code: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    owner_domain: Mapped[str] = mapped_column(Text)
    call_key: Mapped[str] = mapped_column(Text)
    call_key_digest: Mapped[str] = mapped_column(Text)
    input_raw: Mapped[bytes] = mapped_column(LargeBinary)
    request_wire: Mapped[bytes] = mapped_column(LargeBinary)
    request_sha256: Mapped[str] = mapped_column(Text)
    device_code: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(Text)
    route: Mapped[dict] = mapped_column(JSON_VALUE)
    target: Mapped[str] = mapped_column(Text)
    delivery_snapshot: Mapped[dict] = mapped_column(JSON_VALUE)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[str] = mapped_column(Text)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    blocked: Mapped[bool] = mapped_column(Boolean)
    needs_reconciliation: Mapped[bool] = mapped_column(Boolean)
    applied_result_id: Mapped[str | None] = mapped_column(Text)
    ack_record_id: Mapped[UUID | None] = mapped_column(Uuid)
    ack_wire: Mapped[bytes | None] = mapped_column(LargeBinary)


class EcsSendEvidence(Base):
    __tablename__ = "ecs_send_evidence"
    command_code: Mapped[str] = mapped_column(Text, primary_key=True)
    lease_id: Mapped[str] = mapped_column(Text, primary_key=True)
    attempt_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_id: Mapped[UUID | None] = mapped_column(Uuid)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    late: Mapped[bool | None] = mapped_column(Boolean)


class EcsResultEvidence(Base):
    __tablename__ = "ecs_result_evidence"
    command_code: Mapped[str] = mapped_column(Text, primary_key=True)
    semantic_digest: Mapped[str] = mapped_column(Text, primary_key=True)
    effective_payload: Mapped[dict] = mapped_column(JSON_VALUE)
    applied: Mapped[bool] = mapped_column(Boolean)
    http_status: Mapped[int] = mapped_column(Integer)
    response_wire: Mapped[bytes] = mapped_column(LargeBinary)
    response_sha256: Mapped[str] = mapped_column(Text)
    attempt_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_id: Mapped[int | None] = mapped_column(BigInteger)


class RuntimeRetentionHold(Base):
    __tablename__ = "integration_runtime_retention_hold"
    subject_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_domain: Mapped[str] = mapped_column(Text, primary_key=True)
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
