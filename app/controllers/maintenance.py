from fastapi import APIRouter, Depends

from app.core import SessionDep, get_settings, require_permission
from app.schemas.audit import ArchiveResult
from app.services import audit_service

router = APIRouter(prefix="/admin/maintenance", tags=["maintenance"])


@router.post(
    "/archive-audit",
    response_model=ArchiveResult,
    dependencies=[Depends(require_permission("admin:maintenance:archive_audit"))],
)
async def archive_audit(session: SessionDep) -> ArchiveResult:
    """手动触发审计日志归档(与每日定时任务同一逻辑),便于验收。"""
    archived = await audit_service.archive_old(session, get_settings().AUDIT_RETENTION_DAYS)
    return ArchiveResult(archived=archived)
