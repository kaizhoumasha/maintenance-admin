from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DeviceRoute(TimestampMixin, Base):
    """Immutable registration evidence; never depends on a deletable configuration row."""

    __tablename__ = "device_routes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_code: Mapped[str] = mapped_column(String(100), index=True)
    ecs_origin: Mapped[str] = mapped_column(String(255))
    created_by: Mapped[int | None] = mapped_column()


class Device(TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_code: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    work_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_lines.id", ondelete="RESTRICT"), index=True
    )
    description: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    current_route_id: Mapped[str] = mapped_column(
        ForeignKey("device_routes.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
