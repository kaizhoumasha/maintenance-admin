import base64
import json
import re
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.schemas.base import DatabaseId, PageParams
from app.schemas.query import CursorParams


async def paginate(session: AsyncSession, stmt: Select[Any], params: PageParams) -> dict[str, Any]:
    """对已带 order_by 的 ORM 查询分页,返回可直接交给 Page[T] 校验的字典。"""
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = []
    if total:
        items = list(await session.scalars(stmt.offset(params.offset).limit(params.page_size)))
    return {"items": items, "total": total or 0, "page": params.page, "page_size": params.page_size}


def _encode_cursor(row_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"id": row_id}).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        if len(cursor) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ValueError
        data = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(data, dict):
            raise ValueError
        row_id = TypeAdapter(DatabaseId).validate_python(data.get("id"), strict=True)
        if _encode_cursor(row_id) != cursor:  # 拒绝空白、多余键、尾部多余位等非规范形式
            raise ValueError
        return row_id
    except (ValueError, UnicodeError, ValidationError):
        raise AppError(ErrorCode.INVALID_REQUEST, "cursor 无效") from None


async def paginate_cursor(
    session: AsyncSession, stmt: Select[Any], id_col: Any, params: CursorParams
) -> dict[str, Any]:
    if params.cursor is not None:
        stmt = stmt.where(id_col < _decode_cursor(params.cursor))
    rows = list(
        await session.scalars(stmt.order_by(None).order_by(id_col.desc()).limit(params.limit + 1))
    )
    # 多取一条只判断有无下一页,游标必须指向最后返回的行。
    cursor = (
        _encode_cursor(getattr(rows[params.limit - 1], id_col.key))
        if len(rows) > params.limit
        else None
    )
    return {"items": rows[: params.limit], "next_cursor": cursor}
