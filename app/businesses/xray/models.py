from sqlalchemy import JSON, BigInteger, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Reel(Base):
    __tablename__ = "xray_reels"
    __table_args__ = (
        UniqueConstraint("work_line_id", "device_id", "reel_id", name="xray_reel_identity"),
        Index("xray_reel_due", "pending", "next_check_at_ms", "id"),
        Index("xray_reel_arm_held", "work_line_id", "arm_device_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    work_line_id: Mapped[int]
    workline_code: Mapped[str] = mapped_column(String(50))
    device_id: Mapped[int]
    device_code: Mapped[str] = mapped_column(String(100))
    reel_id: Mapped[str] = mapped_column(String(36))
    pending: Mapped[bool] = mapped_column(default=True)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
    conflict: Mapped[str | None] = mapped_column(String(64))
    wait_reason: Mapped[str | None] = mapped_column(String(64))
    arm_device_code: Mapped[str | None] = mapped_column(String(100), unique=True)
    actions: Mapped[dict] = mapped_column(JSON, default=dict)
    destination: Mapped[dict | None] = mapped_column(JSON)


class ReelEvent(Base):
    __tablename__ = "xray_reel_events"
    __table_args__ = (
        Index("xray_event_device_identity", "device_id", "event_id"),
        Index("xray_event_reel", "reel_key", "receipt_id"),
    )

    receipt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    work_line_id: Mapped[int]
    device_id: Mapped[int]
    reel_key: Mapped[str] = mapped_column(ForeignKey(Reel.id))
    event_id: Mapped[str] = mapped_column(String(36))
    stage: Mapped[str] = mapped_column(String(10))
    timestamp: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)


class ReelOperation(Base):
    __tablename__ = "xray_reel_operations"
    __table_args__ = (UniqueConstraint("reel_key", "stage", "round", name="xray_operation_round"),)

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reel_key: Mapped[str] = mapped_column(ForeignKey(Reel.id))
    stage: Mapped[str] = mapped_column(String(10))
    round: Mapped[int]
    operation: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(BigInteger)
    source_event_id: Mapped[str | None] = mapped_column(String(36))
    response: Mapped[dict | None] = mapped_column(JSON)
    retry_at: Mapped[int | None] = mapped_column(BigInteger)
    rack_basis: Mapped[dict | None] = mapped_column(JSON)
    rack_preparation_id: Mapped[int | None]


class RackPreparation(Base):
    __tablename__ = "xray_rack_preparations"
    __table_args__ = (
        Index("xray_preparation_point", "work_line_id", "point", "id"),
        Index("xray_preparation_due", "pending", "next_check_at_ms", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_line_id: Mapped[int]
    point: Mapped[str] = mapped_column(String(100))
    operation_id: Mapped[str] = mapped_column(ForeignKey(ReelOperation.operation_id), unique=True)
    old_job_id: Mapped[str | None] = mapped_column(String(36))
    incoming_job_id: Mapped[str | None] = mapped_column(String(36))
    action_id: Mapped[str | None] = mapped_column(String(36))
    pending: Mapped[bool] = mapped_column(default=True)
    next_check_at_ms: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"))
