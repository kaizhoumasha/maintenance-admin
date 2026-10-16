from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.base import PageParams


async def paginate(session: AsyncSession, stmt: Select[Any], params: PageParams) -> dict[str, Any]:
    """对已带 order_by 的 ORM 查询分页,返回可直接交给 Page[T] 校验的字典。"""
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = []
    if total:
        items = list(await session.scalars(stmt.offset(params.offset).limit(params.page_size)))
    return {"items": items, "total": total or 0, "page": params.page, "page_size": params.page_size}
