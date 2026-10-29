"""Read platform receipts and an explicitly injected business projection in one snapshot."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.integrations.callback_receipts import find_receipt
from app.integrations.ecs_callback.receiver import OPERATION
from app.schemas.dev_ecs_event import DevEcsEventReceipt, DevEcsEventState

type BusinessReader = Callable[[AsyncSession, str], Awaitable[dict[str, Any] | None]]


async def event_state(
    factory, digest: str, read_business: BusinessReader | None = None
) -> DevEcsEventState:
    async with factory() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
        row = await find_receipt(session, OPERATION, digest)
        receipt = (
            DevEcsEventReceipt(
                status_code=row.status_code,
                response=row.body,
                diagnostic=row.diagnostic,
                created_at=row.created_at,
            )
            if row is not None
            else None
        )
        business = await read_business(session, digest) if read_business is not None else None
        if receipt is None and business is None:
            raise AppError(ErrorCode.NOT_FOUND, "没有该事件摘要的收据或业务记录")
        return DevEcsEventState(digest=digest, receipt=receipt, business=business)
