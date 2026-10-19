from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logger
from app.models import AuditLog, AuditLogArchive
from app.schemas.base import DatabaseId
from app.schemas.query import CursorParams, FilterGroup
from app.services.filtering import FieldSpec, apply_filter
from app.services.pagination import paginate_cursor


def _fields(model: type[AuditLog] | type[AuditLogArchive]) -> dict[str, FieldSpec]:
    return {
        "user_id": FieldSpec(model.user_id, DatabaseId, ("eq", "in", "is_null", "not_null")),
        "resource_id": FieldSpec(model.resource_id, DatabaseId, ("eq", "in")),
        "action": FieldSpec(model.action, str, ("eq", "in")),
        "resource": FieldSpec(model.resource, str, ("eq", "in")),
        "created_at": FieldSpec(model.created_at, datetime, ("ge", "le", "between")),
    }


_FIELDS = {model: _fields(model) for model in (AuditLog, AuditLogArchive)}


async def _list(
    session: AsyncSession,
    model: type[AuditLog] | type[AuditLogArchive],
    params: CursorParams,
    filters: FilterGroup | None,
) -> dict[str, Any]:
    stmt = apply_filter(select(model), filters, _FIELDS[model])
    return await paginate_cursor(session, stmt, model.id, params)


async def list_audit_logs(
    session: AsyncSession, params: CursorParams, filters: FilterGroup | None = None
) -> dict[str, Any]:
    return await _list(session, AuditLog, params, filters)


async def list_archived_logs(
    session: AsyncSession, params: CursorParams, filters: FilterGroup | None = None
) -> dict[str, Any]:
    return await _list(session, AuditLogArchive, params, filters)


async def archive_old(session: AsyncSession, days: int) -> int:
    """把 days 天前的审计日志搬到归档表并从源表删除(同一事务),返回搬运条数。

    shortcut: 一次搬完,积压很大时是长事务;需要时改成按 (created_at, id) 分批删除。
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cols = list(AuditLog.__table__.c)
    old = AuditLog.created_at < cutoff
    await session.execute(
        insert(AuditLogArchive).from_select([c.name for c in cols], select(*cols).where(old))
    )
    result = await session.execute(delete(AuditLog).where(old))
    await session.commit()
    logger.info("审计日志归档完成: 搬运 %d 条", result.rowcount)
    return result.rowcount
