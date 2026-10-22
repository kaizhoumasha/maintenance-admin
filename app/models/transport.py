from datetime import datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TransportTask(Base):
    __tablename__ = "transport_task"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('RACK_MOVE','RACK_ROTATE','BIN_MOVE','BIN_EXCHANGE')", name="kind"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_request_id: Mapped[str] = mapped_column(Text, unique=True)
    transport_task_id: Mapped[str] = mapped_column(Text, unique=True)
    operation_id: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    input_digest: Mapped[str] = mapped_column(String(64))
    workline_id: Mapped[str] = mapped_column(Text)
    station_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 以下列用于 WMS 回调的应用(见回调接收设计 §4.2);JSON 列一律整体重新赋值,不要原地修改
    frozen: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    member_positions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    outcome_status: Mapped[str | None] = mapped_column(String(16))
    applied_outcome: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    applied_outcome_revision: Mapped[int | None] = mapped_column(BigInteger)
    outcome_digests: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    pending_outcome: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
