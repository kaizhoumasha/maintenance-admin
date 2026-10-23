from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class CallbackReceipt(Base):
    __tablename__ = "callback_receipt"
    __table_args__ = (
        UniqueConstraint("operation", "operation_id"),
        Index("callback_receipt_retention_idx", "created_at", "id"),
        CheckConstraint(
            "(compare_mode = 'RAW' AND effective_digest IS NULL) OR "
            "(compare_mode = 'EFFECTIVE' AND effective_digest IS NOT NULL)",
            name="compare_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(256))
    operation_id: Mapped[str] = mapped_column(String(64))
    raw_digest: Mapped[str] = mapped_column(String(64))
    effective_digest: Mapped[str | None] = mapped_column(String(64))
    compare_mode: Mapped[str] = mapped_column(String(9))
    status_code: Mapped[int]
    body: Mapped[dict] = mapped_column(JSON)
    resp_timestamp: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    diagnostic: Mapped[list | None] = mapped_column(JSON(none_as_null=True))


class CallbackSnapshot(Base):
    __tablename__ = "callback_snapshot"
    __table_args__ = (Index("callback_snapshot_retention_idx", "received_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_b64: Mapped[str] = mapped_column(Text)
    truncated: Mapped[bool]
    http_status: Mapped[int]
    response_body: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    outcome: Mapped[str] = mapped_column(String(32))
    source: Mapped[str | None] = mapped_column(String(16))
