from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallbackReceipt

LOCK_TIMEOUT = "5s"  # PostgreSQL 回调事务等待行锁的上限,超时整体回滚并返回 503


async def insert_receipt(
    session: AsyncSession, *, lock_timeout: str = LOCK_TIMEOUT, **values
) -> int | None:
    """写收据占住身份;同 (operation, operation_id) 已存在则返回 None。"""
    postgresql = session.get_bind().dialect.name == "postgresql"
    if postgresql:  # 等待行锁有上限:拿不到锁就整体回滚,对端用原身份重发
        await session.execute(text(f"SET LOCAL lock_timeout = '{lock_timeout}'"))
    insert = pg_insert if postgresql else sqlite_insert
    return await session.scalar(
        insert(CallbackReceipt)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["operation", "operation_id"])
        .returning(CallbackReceipt.id)
    )


async def find_receipt(
    session: AsyncSession, operation: str, operation_id: str
) -> CallbackReceipt | None:
    return await session.scalar(
        select(CallbackReceipt).where(
            CallbackReceipt.operation == operation, CallbackReceipt.operation_id == operation_id
        )
    )
