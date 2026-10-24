from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EcsCommand(Base):
    __tablename__ = "ecs_command"
    __table_args__ = (UniqueConstraint("owner_domain", "call_key_digest"),)
    command_code: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_domain: Mapped[str] = mapped_column(Text)
    call_key: Mapped[str] = mapped_column(Text)
    call_key_digest: Mapped[str] = mapped_column(String(64))
    input_raw: Mapped[bytes] = mapped_column(LargeBinary)
    device_code: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(Text)
    route: Mapped[dict] = mapped_column(JSON)
    request_raw: Mapped[bytes] = mapped_column(LargeBinary)
    target: Mapped[str] = mapped_column(Text)
    delivery_snapshot: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    delivery_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    hold_reason: Mapped[str | None] = mapped_column(String(64))
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_reconciliation: Mapped[bool] = mapped_column(Boolean, default=False)
    applied_result_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EcsSendEvidence(Base):
    __tablename__ = "ecs_send_evidence"
    __table_args__ = (UniqueConstraint("command_code", "lease_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_code: Mapped[str] = mapped_column(String(36), index=True)
    lease_id: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32), default="OPEN_ATTEMPT")
    response_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    http_status: Mapped[int | None] = mapped_column(Integer)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EcsResultEvidence(Base):
    __tablename__ = "ecs_result_evidence"
    __table_args__ = (UniqueConstraint("command_code", "digest"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command_code: Mapped[str] = mapped_column(String(36), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    core: Mapped[dict] = mapped_column(JSON)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JSON)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
