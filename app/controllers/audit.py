from typing import Any

from fastapi import Depends

from app.core import SessionDep, require_permission
from app.core.routing import new_router
from app.schemas.audit import AuditLogArchiveRead, AuditLogRead
from app.schemas.base import Page, PageQuery
from app.services import audit_service

router = new_router(prefix="/audit-logs", tags=["audit-logs"])


@router.get(
    "",
    response_model=Page[AuditLogRead],
    dependencies=[Depends(require_permission("admin:audit:read"))],
)
async def list_audit_logs(session: SessionDep, params: PageQuery) -> dict[str, Any]:
    return await audit_service.list_audit_logs(session, params)


@router.get(
    "/archive",
    response_model=Page[AuditLogArchiveRead],
    dependencies=[Depends(require_permission("admin:audit:read_archive"))],
)
async def list_archived_logs(session: SessionDep, params: PageQuery) -> dict[str, Any]:
    return await audit_service.list_archived_logs(session, params)
