from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy import text

from app.core import error_responses, require_permission
from app.core.request_context import get_trace_id
from app.core.routing import new_router
from app.models import User
from app.schemas.integration_history import (
    ExchangeDetail,
    ExchangeListParams,
    ExchangeSummary,
    RecordListParams,
    RecordSummary,
)
from app.schemas.query import CursorPage
from app.services import audit_service
from app.services import integration_history_service as history

router = new_router(prefix="/integrations", tags=["integration-history"])
read_history = require_permission("integration:history:read")
read_payload = require_permission("integration:payload:read")


@asynccontextmanager
async def _reader(request):
    async with request.app.state.integration_history_session_factory() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
        yield session


@router.get(
    "/exchanges", response_model=CursorPage[ExchangeSummary], responses=error_responses(503)
)
async def list_exchanges(
    request: Request,
    params: Annotated[ExchangeListParams, Query()],
    user: Annotated[User, Depends(read_history)],
):
    async with _reader(request) as session:
        return await history.list_exchanges(session, params)


@router.get(
    "/exchanges/{exchange_id}", response_model=ExchangeDetail, responses=error_responses(404, 503)
)
async def get_exchange(
    request: Request, exchange_id: UUID, user: Annotated[User, Depends(read_history)]
):
    async with _reader(request) as session:
        return await history.get_exchange(session, exchange_id)


@router.get(
    "/exchanges/{exchange_id}/records",
    response_model=CursorPage[RecordSummary],
    responses=error_responses(404, 503),
)
async def list_records(
    request: Request,
    exchange_id: UUID,
    params: Annotated[RecordListParams, Query()],
    user: Annotated[User, Depends(read_history)],
):
    async with _reader(request) as session:
        return await history.list_records(session, exchange_id, params)


@router.get(
    "/records/{record_id}/body",
    response_class=Response,
    responses={
        **error_responses(404, 503),
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        },
    },
)
async def get_body(
    request: Request,
    record_id: UUID,
    background: BackgroundTasks,
    user: Annotated[User, Depends(read_payload)],
):
    async with _reader(request) as session:
        body = await history.get_body(session, record_id)
    background.add_task(
        audit_service.record_payload_read,
        request.app.state.audit_session_factory,
        user_id=user.id,
        record_id=record_id,
        trace_id=get_trace_id(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return Response(
        body.raw_body,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "X-Integration-Body-State": body.body_state,
            "X-Integration-Body-Sha256": body.body_sha256,
            "X-Integration-Stored-Bytes": str(body.stored_bytes),
        },
    )
