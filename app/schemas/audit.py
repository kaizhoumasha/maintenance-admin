from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None = None
    action: str
    resource: str | None = None
    resource_id: int | None = None
    detail: dict[str, Any] | None
    ip: str | None = None
    created_at: datetime


class AuditLogArchiveRead(AuditLogRead):
    archived_at: datetime


class ArchiveResult(BaseModel):
    archived: int
