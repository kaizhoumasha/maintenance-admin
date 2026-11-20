"""HTTP route for ECS synchronous request/decision exchange."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from functools import partial

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import Response

from app.core import get_settings
from app.core.database import session_factory
from app.core.identifiers import uuid7
from app.core.ratelimit import RateLimit
from app.core.request_body import read_body_capped
from app.diagnostics import log_exception
from app.integrations.ecs_request.receiver import (
    MAX_BODY_BYTES,
    EcsRequestResponse,
    accept_ecs_request,
    continue_ecs_request,
)
from app.integrations.ecs_request.registry import requests
from app.integrations.http_exchanges import capture_callback
from app.integrations.wms_request.results import get_result
from app.models import ReliableMessage
from app.reliable.config import parse_reliable_config

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ECS callbacks"], dependencies=[Depends(RateLimit(None))])


@router.post(
    "/callback/request",
    response_class=Response,
    responses={
        400: {"description": "INVALID_ENVELOPE or UNKNOWN_REQUEST"},
        403: {"description": "FORBIDDEN_SOURCE"},
        413: {"description": "PAYLOAD_TOO_LARGE"},
        503: {"description": "TEMPORARILY_UNAVAILABLE"},
    },
)
async def request_callback(request: Request, background_tasks: BackgroundTasks) -> Response:
    result = await receive_request(request)
    return Response(
        result.prepared_body,
        status_code=result.status_code,
        media_type="application/json",
        background=background_tasks,
    )


async def receive_request(request: Request) -> EcsRequestResponse:
    received_at = datetime.now(UTC)
    exchange_id = uuid7()
    factory = getattr(request.app.state, "callback_session_factory", session_factory)
    raw = await read_body_capped(request, MAX_BODY_BYTES)
    if len(raw) > MAX_BODY_BYTES:
        result = EcsRequestResponse(413, {"code": 413, "message": "PAYLOAD_TOO_LARGE"}, "too_large")
        await _capture_request(factory, request, received_at, exchange_id, raw, result)
        return result
    authorize = getattr(request.app.state, "ecs_event_authorizer", None)
    if authorize is not None:
        authorize = partial(
            authorize,
            source_ip=request.client.host if request.client else "",
            real_callback=True,
        )
    try:
        async with factory() as session, session.begin():
            result = await accept_ecs_request(
                session,
                raw,
                resolve_binding=getattr(request.app.state, "ecs_event_binding_resolver", None),
                authorize_request=authorize,
                runtime=getattr(request.app.state, "business_runtime", None),
                on_committed=getattr(request.app.state, "business_event_committed", None),
                on_business_committed=getattr(request.app.state, "business_committed", None),
                resolve_input_target=getattr(request.app.state, "ecs_event_input_target", None),
                attempt_id=str(exchange_id),
            )
    except Exception as error:
        log_exception(
            logger, "ecs request receive failed", error, component="http", phase="ecs_request"
        )
        result = EcsRequestResponse(
            503, {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}, "error"
        )
        await _capture_request(factory, request, received_at, exchange_id, raw, result)
        return result
    if not result.deferred:
        await _capture_request(factory, request, received_at, exchange_id, raw, result)
        return result
    deferred = await _read_deferred(factory, result.receipt_id)
    if deferred is None:
        await _capture_request(factory, request, received_at, exchange_id, raw, result)
        return result
    terminal = await wait_for_request(
        factory,
        result.digest,
        deferred,
        budget_ms=await request_budget_ms(factory, deferred.message_id),
    )
    if terminal is None:
        await _capture_request(factory, request, received_at, exchange_id, raw, result)
        return result
    try:
        async with factory() as session, session.begin():
            result = await continue_ecs_request(
                session,
                identity=result.digest,
                deferred=deferred,
                resolve_binding=getattr(request.app.state, "ecs_event_binding_resolver", None),
                runtime=getattr(request.app.state, "business_runtime", None),
                resolve_input_target=getattr(request.app.state, "ecs_event_input_target", None),
            )
    except Exception as error:
        log_exception(
            logger, "ecs request continue failed", error, component="http", phase="ecs_request"
        )
        result = EcsRequestResponse(
            503, {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}, "error"
        )
    await _capture_request(factory, request, received_at, exchange_id, raw, result)
    return result


async def _capture_request(factory, request, received_at, exchange_id, raw, result):
    await capture_callback(
        factory,
        identity=exchange_id,
        source="ecs_request",
        request=request,
        started_at=received_at,
        raw=raw,
        limit=MAX_BODY_BYTES,
        result=result,
    )


async def request_budget_ms(factory, message_id: int) -> float:
    try:
        async with factory() as session:
            message = await session.get(ReliableMessage, message_id)
        config = parse_reliable_config(get_settings())
        target = config.targets.get(message.target) if message is not None else None
        return (target.timeout_ms if target is not None else 10000) + config.poll_interval_s * 1000
    except Exception:
        return 11000


async def _read_deferred(factory, receipt_id):
    from sqlalchemy import select

    from app.integrations.callback_receipts import active_receipt, receipt_model

    if receipt_id is None:
        return None
    table = receipt_model()
    async with factory() as session:
        context = await session.scalar(
            select(table.event_context).where(active_receipt(table), table.receipt_id == receipt_id)
        )
    data = (context or {}).get("deferred_request")
    if not data:
        return None
    from app.integrations.ecs_request.registry import DeferredRequest

    return DeferredRequest(**data)


async def wait_for_request(
    factory, identity: str | None, deferred, *, budget_ms: float | None = None
):
    if identity is None:
        return None
    if budget_ms is None:
        budget_ms = 11000
    deadline = time.monotonic() + budget_ms / 1000
    while time.monotonic() < deadline:
        async with factory() as session:
            from app.integrations.callback_receipts import find_receipt_state

            receipt = await find_receipt_state(session, "ecs.request", identity)
            if receipt is not None and receipt.prepared_body is not None:
                return receipt
            result = await get_result(
                session,
                message_id=deferred.message_id,
                operation=deferred.operation,
                operation_id=deferred.operation_id,
            )
            if result.delivery_uncertain or (
                result.response_state == "NONE" and not result.delivery_terminal
            ):
                pass
            else:
                return result
        await asyncio.sleep(0.2)
    return None


def include_ecs_request(app: FastAPI, *, environment: str, prefix: str = "/api/v1") -> None:
    if requests() and getattr(app.state, "ecs_event_binding_resolver", None) is None:
        raise ValueError("Formal ECS request handlers require a binding resolver")
    if any(
        not item.registration_source.local_only
        and item.registration_source.module.startswith("app.businesses._components.")
        for item in requests().values()
    ) and getattr(app.state, "ecs_event_input_target", None) is None:
        raise ValueError("Shared ECS requests require an input target resolver")
    app.include_router(router, prefix=prefix)
