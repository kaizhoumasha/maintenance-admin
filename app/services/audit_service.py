from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logger
from app.models import AuditLog, AuditLogArchive
from app.schemas.base import PageParams
from app.services.pagination import paginate


async def list_audit_logs(session: AsyncSession, params: PageParams) -> dict[str, Any]:
    return await paginate(session, select(AuditLog).order_by(AuditLog.id.desc()), params)


async def list_archived_logs(session: AsyncSession, params: PageParams) -> dict[str, Any]:
    return await paginate(
        session, select(AuditLogArchive).order_by(AuditLogArchive.id.desc()), params
    )


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
