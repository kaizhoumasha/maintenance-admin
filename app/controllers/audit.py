from typing import Annotated, Any

from fastapi import Depends, Query

from app.core import SessionDep, require_permission
from app.core.routing import new_router
from app.schemas.audit import AuditLogArchiveRead, AuditLogRead
from app.schemas.query import CursorPage, CursorParams, parse_filter
from app.services import audit_service

router = new_router(prefix="/audit-logs", tags=["audit-logs"])


@router.get(
    "",
    response_model=CursorPage[AuditLogRead],
    dependencies=[Depends(require_permission("admin:audit:read"))],
)
async def list_audit_logs(
    session: SessionDep, params: Annotated[CursorParams, Query()]
) -> dict[str, Any]:
    return await audit_service.list_audit_logs(session, params, parse_filter(params.filter))


@router.get(
    "/archive",
    response_model=CursorPage[AuditLogArchiveRead],
    dependencies=[Depends(require_permission("admin:audit:read_archive"))],
)
async def list_archived_logs(
    session: SessionDep, params: Annotated[CursorParams, Query()]
) -> dict[str, Any]:
    return await audit_service.list_archived_logs(session, params, parse_filter(params.filter))
