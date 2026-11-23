import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dev_scenarios import DevOutcome, DevRequest, DevScenario
from app.core.errors import AppError, ErrorCode
from app.integrations.history import read_bodies
from app.integrations.runtime_authority import enabled
from app.models import ReliableMessage
from app.schemas.dev_console import DevMessageOut


async def commit_or_map(session: AsyncSession, work, errors: dict[type[Exception], ErrorCode]):
    """等待 `work` 并提交,`errors` 里的异常回滚并转 AppError。

    get_session 不提交,不提交的话发送器看不到消息。
    """
    try:
        outcome = await work
        await session.commit()
    except tuple(errors) as error:
        await session.rollback()
        code = next(c for t, c in errors.items() if isinstance(error, t))
        raise AppError(code, str(error)) from error
    return outcome


async def submit(session: AsyncSession, scenario: DevScenario, req: DevRequest) -> DevOutcome:
    """调用场景 handler 并提交。"""
    return await commit_or_map(session, scenario.handler(session, req), scenario.errors)


def _decode(raw: bytes | None) -> Any:
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def message_out(m: ReliableMessage, request, response, last_response) -> DevMessageOut:
    return DevMessageOut(
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
        request=_decode(request),
        response=_decode(response),
        last_response=_decode(last_response),
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


async def messages_where(
    session: AsyncSession, column, value: str, not_found: str
) -> list[DevMessageOut]:
    query = (
        select(ReliableMessage)
        .where(column == value)
        .order_by(ReliableMessage.id)
        .execution_options(populate_existing=True)
    )
    messages = (await session.scalars(query)).all()
    if not messages:
        raise AppError(ErrorCode.NOT_FOUND, not_found)
    if enabled():
        return [
            message_out(m, m.frozen_request_wire, m.terminal_response_wire, m.last_response_wire)
            for m in messages
        ]
    bodies = await read_bodies(
        session,
        record_ids=tuple(
            {
                identity
                for m in messages
                for identity in (
                    m.request_record_id,
                    m.terminal_response_record_id,
                    m.last_response_record_id,
                )
                if identity is not None
            }
        ),
    )

    def wire(identity):
        body = bodies.get(identity)
        return body.raw_body if body is not None else None

    return [
        message_out(
            m,
            wire(m.request_record_id),
            wire(m.terminal_response_record_id),
            wire(m.last_response_record_id),
        )
        for m in messages
    ]


async def message_status(session: AsyncSession, business_key: str) -> list[DevMessageOut]:
    return await messages_where(
        session, ReliableMessage.business_key, business_key, "没有该 business_key 的出站消息"
    )
