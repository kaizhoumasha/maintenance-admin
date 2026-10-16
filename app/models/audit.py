from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditColumns:
    """audit_logs 与 audit_logs_archive 共用的列;id 和 created_at 由各表自己声明。"""

    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(255), index=True)
    trace_id: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    resource_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    resource: Mapped[str | None] = mapped_column(String(255), nullable=True)

    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AuditLog(AuditColumns, Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    # 归档任务按 created_at 范围扫描,必须有索引
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AuditLogArchive(AuditColumns, Base):
    __tablename__ = "audit_logs_archive"
    # 沿用原表 id 和 created_at(不自增、不取当前时间);重复归档同一行会撞主键而回滚
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
