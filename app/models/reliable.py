from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ReliableMessage(Base):
    __tablename__ = "reliable_message"
    __table_args__ = (
        UniqueConstraint(
            "target", "operation", "operation_id", name="reliable_message_identity_key"
        ),
        CheckConstraint("status IN ('PENDING','SENDING','DONE','FAILED','EXPIRED')", name="status"),
        Index("reliable_message_due_idx", "status", "next_attempt_at"),
        Index("reliable_message_expiry_idx", "status", "expires_at"),
        Index("reliable_message_lease_idx", "status", "lease_until"),
        Index("reliable_message_retention_idx", "status", "updated_at"),
        Index("reliable_message_business_key_idx", "business_key"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str] = mapped_column(Text)
    request_raw: Mapped[bytes] = mapped_column(LargeBinary)
    response_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    fail_reason: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_failure_kind: Mapped[str | None] = mapped_column(String(64))
    last_verdict: Mapped[str | None] = mapped_column(String(32))
    last_response_raw: Mapped[bytes | None] = mapped_column(LargeBinary)
    lease_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_key: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
