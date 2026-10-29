"""Durable scan facts and pending work owned by coarse sorting."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CoarseSortingScan(Base):
    __tablename__ = "coarse_sorting_scan"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_digest: Mapped[str] = mapped_column(String(64), unique=True)
    # Snapshot identities deliberately survive receipt/configuration retention.
    receipt_id: Mapped[int]
    work_line_id: Mapped[int]
    workline_code: Mapped[str]
    device_id: Mapped[int]
    device_code: Mapped[str]
    device_role: Mapped[str]
    device_version: Mapped[int]
    work_line_version: Mapped[int]
    binding_revision: Mapped[str]
    timestamp: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pending: Mapped[bool]
    wait_reason: Mapped[str]
