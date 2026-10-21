from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
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
