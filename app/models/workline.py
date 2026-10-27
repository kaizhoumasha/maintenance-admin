from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WorkLine(TimestampMixin, Base):
    __tablename__ = "work_lines"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    line_code: Mapped[str] = mapped_column(String(50), unique=True)
    line_name: Mapped[str] = mapped_column(String(100))
    business_code: Mapped[str | None] = mapped_column(String(100))
    zone_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    device_bindings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    position_bindings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
