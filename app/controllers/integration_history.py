from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from fastapi import BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from app.core import error_responses, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.request_context import get_trace_id
from app.core.routing import new_router
from app.integrations.runtime_authority import enabled
from app.models import User
from app.schemas.integration_history import (
    ExchangeDetail,
    ExchangeListParams,
    ExchangeSummary,
    HttpExchangeDetail,
    HttpExchangeSummary,
    HttpRecordSummary,
    RecordListParams,
    RecordSummary,
)
from app.schemas.query import CursorPage
from app.services import audit_service, http_exchange_history
from app.services import integration_history_service as history
from app.services.http_exchange_diagnostics import EXCHANGES

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
    "/exchanges",
    response_model=CursorPage[HttpExchangeSummary | ExchangeSummary],
    responses=error_responses(503),
)
async def list_exchanges(
    request: Request,
    params: Annotated[ExchangeListParams, Query()],
    user: Annotated[User, Depends(read_history)],
):
    async with _reader(request) as session:
        return await (http_exchange_history if enabled() else history).list_exchanges(
            session, params
        )


@router.get(
    "/exchanges/{exchange_id}",
    response_model=HttpExchangeDetail | ExchangeDetail,
    responses=error_responses(404, 503),
)
async def get_exchange(
    request: Request, exchange_id: UUID, user: Annotated[User, Depends(read_history)]
):
    async with _reader(request) as session:
        return await (http_exchange_history if enabled() else history).get_exchange(
            session, exchange_id
        )


@router.get(
    "/exchanges/{exchange_id}/records",
    response_model=CursorPage[HttpRecordSummary | RecordSummary],
    responses=error_responses(404, 503),
)
async def list_records(
    request: Request,
    exchange_id: UUID,
    params: Annotated[RecordListParams, Query()],
    user: Annotated[User, Depends(read_history)],
):
    async with _reader(request) as session:
        return await (http_exchange_history if enabled() else history).list_records(
            session, exchange_id, params
        )


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
    exchange_id = side = peer = None
    async with _reader(request) as session:
        if enabled():
            exchange_id, peer, side = await http_exchange_history.locate_body(session, record_id)
            body, state, digest, size = await _body(session, peer, exchange_id, side, "wire")
        else:
            stored = await history.get_body(session, record_id)
            body, state, digest, size = (
                stored.raw_body,
                stored.body_state,
                stored.body_sha256,
                stored.stored_bytes,
            )
    _audit_body(
        request,
        background,
        user,
        record_id=record_id,
        exchange_id=exchange_id,
        side=side,
        peer=peer,
        representation="wire" if side else None,
    )
    return _body_response(body, state, digest, size, "wire")


@router.get("/diagnostics")
async def get_diagnostics(
    request: Request,
    peer: Literal["WMS", "ECS", "TRANSPORT"],
    reference: Annotated[str, Query(min_length=1, max_length=512)],
    user: Annotated[User, Depends(read_history)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: UUID | None = None,
):
    from app.services.http_exchange_diagnostics import diagnostics

    async with _reader(request) as session:
        return await diagnostics(
            session, peer=peer, reference=reference, limit=limit, before=before
        )


@router.get(
    "/http-exchanges/{peer}/{exchange_id}/{side}",
    response_class=Response,
    responses=error_responses(404, 503),
)
async def get_http_body(
    request: Request,
    peer: Literal["WMS", "ECS", "TRANSPORT"],
    exchange_id: UUID,
    side: Literal["request", "response"],
    background: BackgroundTasks,
    user: Annotated[User, Depends(read_payload)],
    representation: Literal["wire", "payload"] = "wire",
):
    async with _reader(request) as session:
        body, state, digest, size = await _body(session, peer, exchange_id, side, representation)
    _audit_body(
        request,
        background,
        user,
        record_id=exchange_id,
        exchange_id=exchange_id,
        side=side,
        peer=peer,
        representation=representation,
    )
    return _body_response(body, state, digest, size, representation)


async def _body(session, peer, exchange_id, side, representation):
    model, _ = EXCHANGES[peer]
    row = (
        await session.execute(
            select(
                getattr(model, f"{side}_{representation}"),
                getattr(model, f"{side}_wire_state"),
                getattr(model, f"{side}_wire_sha256"),
                getattr(model, f"{side}_wire_bytes"),
            ).where(model.id == exchange_id)
        )
    ).one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "HTTP 往来不存在")
    body, state, digest, size = row
    if body is None:
        raise AppError(ErrorCode.NOT_FOUND, "正文不可用", detail=state)
    if representation == "wire" and (size != len(body) or digest != sha256(body).hexdigest()):
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "正文完整性校验失败")
    return body, state, digest, size


def _audit_body(
    request, background, user, *, record_id, side, peer, representation, exchange_id=None
):
    background.add_task(
        audit_service.record_payload_read,
        request.app.state.audit_session_factory,
        user_id=user.id,
        record_id=record_id,
        trace_id=get_trace_id(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        side=side,
        peer=peer,
        representation=representation,
        exchange_id=exchange_id,
    )


def _body_response(body, state, digest, size, representation):
    if representation == "payload":
        return JSONResponse(
            body,
            headers={"X-Content-Type-Options": "nosniff", "X-Integration-Body-State": state},
        )
    return Response(
        body,
        media_type="application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "X-Integration-Body-State": state,
            "X-Integration-Body-Sha256": digest,
            "X-Integration-Stored-Bytes": str(size),
        },
    )
