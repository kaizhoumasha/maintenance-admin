from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ReliableMessage(Base):
    __tablename__ = "reliable_message"
    __table_args__ = (
        UniqueConstraint(
            "target", "operation", "operation_id", name="reliable_message_identity_key"
        ),
        CheckConstraint(
            (
                "status IN "
                "('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED','ABANDONED')"
            ),
            name="status",
        ),
        CheckConstraint(
            "(status IN ('STALLED','HELD','ABANDONED') AND next_attempt_at IS NULL) OR "
            "(status NOT IN ('STALLED','HELD','ABANDONED') AND next_attempt_at IS NOT NULL)",
            name="next_attempt",
        ),
        CheckConstraint(
            "(scope_owner IS NULL AND scope_kind IS NULL AND scope_id IS NULL) OR "
            "(scope_owner IS NOT NULL AND scope_owner != '' AND scope_kind IS NOT NULL AND "
            "scope_kind IN ('TASK','LINE') AND scope_id IS NOT NULL AND scope_id != '')",
            name="scope",
        ),
        Index("reliable_message_scope_idx", "scope_owner", "scope_kind", "scope_id"),
        Index("reliable_message_due_idx", "status", "next_attempt_at"),
        Index("reliable_message_expiry_idx", "status", "expires_at"),
        Index("reliable_message_lease_idx", "status", "lease_until"),
        Index("reliable_message_retention_idx", "status", "updated_at"),
        Index("reliable_message_business_key_idx", "business_key"),
        Index("reliable_message_operation_id_idx", "operation_id"),
        *(
            Index(
                f"reliable_message_{column}_idx",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
                sqlite_where=text(f"{column} IS NOT NULL"),
            )
            for column in (
                "exchange_id",
                "request_record_id",
                "terminal_response_record_id",
                "last_response_record_id",
            )
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str] = mapped_column(Text)
    delivery_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    pause_reason: Mapped[str | None] = mapped_column(String(64))
    exchange_id: Mapped[UUID] = mapped_column(Uuid)
    request_record_id: Mapped[UUID] = mapped_column(Uuid)
    frozen_request_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    frozen_request_sha256: Mapped[str | None] = mapped_column(String(64))
    terminal_response_record_id: Mapped[UUID | None] = mapped_column(Uuid)
    last_response_record_id: Mapped[UUID | None] = mapped_column(Uuid)
    terminal_response_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    terminal_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_wire: Mapped[bytes | None] = mapped_column(LargeBinary)
    protocol: Mapped[str | None] = mapped_column(Text)
    superseded_by_message_id: Mapped[int | None] = mapped_column(Integer)
    replacement_blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    fail_reason: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # STALLED 消息没有计划发送时间;其余状态由 next_attempt 约束保证非空。
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_received_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    delivery_uncertain: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_failure_kind: Mapped[str | None] = mapped_column(String(64))
    last_verdict: Mapped[str | None] = mapped_column(String(32))
    lease_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_not_started: Mapped[bool | None] = mapped_column(Boolean)
    scope_owner: Mapped[str | None] = mapped_column(Text)
    scope_kind: Mapped[str | None] = mapped_column(String(4))
    scope_id: Mapped[str | None] = mapped_column(Text)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_key: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
