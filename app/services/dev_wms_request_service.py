import json

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.integrations.wms_request import InvalidRequestPayload, wms_requests
from app.integrations.wms_request.adapter import PROTOCOL_OF
from app.integrations.wms_request.operation import lookup
from app.models import ReliableMessage
from app.reliable import DuplicateMessage, InvalidMessage
from app.schemas.dev_console import DevMessageOut, DevRequestOut
from app.services.dev_submit_service import commit_or_map, messages_where

_ERRORS = {
    InvalidRequestPayload: ErrorCode.INVALID_REQUEST,
    InvalidMessage: ErrorCode.INVALID_REQUEST,
    DuplicateMessage: ErrorCode.CONFLICT,
}


async def send(
    session: AsyncSession, operation: str, operation_id: str, data: dict
) -> DevRequestOut:
    """走真实的 `WmsRequests.send` 入队并提交。"""
    registered = lookup(operation)
    try:
        # 请求体是 JSON:按 JSON 模式严格校验(ISO 字符串可以成为 datetime),再以 Python 对象交给 send
        payload = registered.data_model.model_validate_json(json.dumps(data))
    except ValidationError as error:
        raise AppError(ErrorCode.INVALID_REQUEST, str(error)) from error
    reference = await commit_or_map(
        session,
        wms_requests.send(session, payload, operation_id=operation_id),
        _ERRORS,
    )
    return DevRequestOut(
        operation=operation,
        operation_id=operation_id,
        message_id=reference.message_id,
        status=reference.status,
        target=wms_requests.target(PROTOCOL_OF[registered.endpoint]),
    )


async def message_status(session: AsyncSession, operation_id: str) -> list[DevMessageOut]:
    return await messages_where(
        session, ReliableMessage.operation_id, operation_id, "没有该 operation_id 的请求消息"
    )
