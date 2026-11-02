from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Integer, String, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class WorkLine(TimestampMixin, Base):
    __tablename__ = "work_lines"
    __table_args__ = (
        CheckConstraint(
            "five_rack_request_limit BETWEEN 1 AND 2147483647 AND "
            "five_rack_request_limit = CAST(five_rack_request_limit AS INTEGER)",
            name="ck_work_lines_five_rack_request_limit",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    line_code: Mapped[str] = mapped_column(String(50), unique=True)
    line_name: Mapped[str] = mapped_column(String(100))
    business_code: Mapped[str | None] = mapped_column(String(100))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    five_rack_request_limit: Mapped[int | None] = mapped_column(Integer)
    zone_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    device_bindings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    position_bindings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
