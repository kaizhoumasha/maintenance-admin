import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dev_scenarios import DevOutcome, DevRequest, DevScenario
from app.core.errors import AppError, ErrorCode
from app.models import ReliableMessage
from app.schemas.dev_outbound import DevMessageOut


async def submit(session: AsyncSession, scenario: DevScenario, req: DevRequest) -> DevOutcome:
    """调用场景 handler 并提交:get_session 不提交,不提交的话发送器看不到消息。"""
    try:
        outcome = await scenario.handler(session, req)
        await session.commit()
    except tuple(scenario.errors) as error:
        await session.rollback()
        code = next(c for t, c in scenario.errors.items() if isinstance(error, t))
        raise AppError(code, str(error)) from error
    return outcome


def _decode(raw: bytes | None) -> Any:
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


async def message_status(session: AsyncSession, business_key: str) -> list[DevMessageOut]:
    rows = await session.scalars(
        select(ReliableMessage)
        .where(ReliableMessage.business_key == business_key)
        .order_by(ReliableMessage.id)
        .execution_options(populate_existing=True)
    )
    messages = list(rows)
    if not messages:
        raise AppError(ErrorCode.NOT_FOUND, "没有该 business_key 的出站消息")
    return [
        DevMessageOut(
            message_id=m.id,
            target=m.target,
            operation=m.operation,
            operation_id=m.operation_id,
            status=m.status,
            fail_reason=m.fail_reason,
            attempts=m.attempts,
            not_received_count=m.not_received_count,
            delivery_uncertain=m.delivery_uncertain,
            next_attempt_at=m.next_attempt_at,
            last_attempt_at=m.last_attempt_at,
            last_http_status=m.last_http_status,
            last_failure_kind=m.last_failure_kind,
            last_verdict=m.last_verdict,
            request=_decode(m.request_raw),
            response=_decode(m.response_raw),
            last_response=_decode(m.last_response_raw),
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
        for m in messages
    ]
