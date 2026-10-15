from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def list_audit_logs(
    session: AsyncSession, page: int, page_size: int
) -> tuple[int, list[AuditLog]]:
    total = await session.scalar(select(func.count()).select_from(AuditLog))
    if not total:
        return 0, []

    items = await session.scalars(
        select(AuditLog)
        .order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return total, list(items)
