from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.integrations.wms_request import InvalidRequestPayload, wms_requests
from app.integrations.wms_request.adapter import PROTOCOL_OF
from app.integrations.wms_request.operation import lookup
from app.models import ReliableMessage
from app.reliable import DuplicateMessage
from app.schemas.dev_console import DevMessageOut, DevRequestOut
from app.services.dev_submit_service import message_out


async def send(
    session: AsyncSession, operation: str, operation_id: str, data: dict
) -> DevRequestOut:
    """走真实的 `WmsRequests.send` 入队并提交:get_session 不提交,不提交的话发送器看不到消息。"""
    try:
        reference = await wms_requests.send(session, operation, data, operation_id=operation_id)
        await session.commit()
    except InvalidRequestPayload as error:
        await session.rollback()
        raise AppError(ErrorCode.INVALID_REQUEST, str(error)) from error
    except DuplicateMessage as error:
        await session.rollback()
        raise AppError(ErrorCode.CONFLICT, "同一 operation_id 已用不同的 Payload 入队") from error
    target = wms_requests.target(PROTOCOL_OF[lookup(operation).endpoint])
    return DevRequestOut(
        operation=operation,
        operation_id=operation_id,
        message_id=reference.message_id,
        status=reference.status,
        target=target,
    )


async def message_status(session: AsyncSession, operation_id: str) -> list[DevMessageOut]:
    rows = await session.scalars(
        select(ReliableMessage)
        .where(ReliableMessage.operation_id == operation_id)
        .order_by(ReliableMessage.id)
        .execution_options(populate_existing=True)
    )
    messages = list(rows)
    if not messages:
        raise AppError(ErrorCode.NOT_FOUND, "没有该 operation_id 的请求消息")
    return [message_out(m) for m in messages]
