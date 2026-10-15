from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(255), index=True)
    trace_id: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    resource_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)

    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
