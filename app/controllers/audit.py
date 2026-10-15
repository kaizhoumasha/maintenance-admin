from fastapi import APIRouter, Depends, Query

from app.core import SessionDep, require_permission
from app.schemas.audit import AuditLogPage
from app.services import audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get(
    "",
    response_model=AuditLogPage,
    dependencies=[Depends(require_permission("admin:audit:read"))],
)
async def list_audit_logs(
    session: SessionDep, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
) -> AuditLogPage:
    total, items = await audit_service.list_audit_logs(session, page, page_size)
    return AuditLogPage(total=total, items=items, page=page, page_size=page_size)
