"""Transactional receiver for ECS synchronous requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.core.strict_json import canonical_digest, json_object
from app.integrations import history
from app.integrations.callback_receipts import (
    active_receipt,
    find_receipt_state,
    insert_receipt,
    receipt_model,
    save_receipt_response,
)
from app.integrations.ecs_callback.binding import BindingResolver, DeviceBinding
from app.integrations.ecs_callback.envelope import issues_from
from app.integrations.wms_callback.receiver import CallbackResponse
from app.models import ReliableMessage
from app.transactions import after_commit

from .envelope import EcsRequestEnvelope
from .registry import DeferredRequest, EcsRequestContext, Registered, lookup

MAX_BODY_BYTES = 256 * 1024
OPERATION = "ecs.request"
PROTOCOL = "ecs-request"


@dataclass(frozen=True)
class EcsRequestResponse(CallbackResponse):
    request_type: str | None = None
    device_code: str | None = None
    digest: str | None = None


class RequestHandlerIncomplete(ValueError):
    pass


def _request(envelope: EcsRequestEnvelope) -> dict:
    return envelope.model_dump(include={"device_code", "request_type", "timestamp", "data"})


def request_identity(envelope: EcsRequestEnvelope) -> str:
    return canonical_digest(_request(envelope))


def _invalid(issues: list[dict]) -> EcsRequestResponse:
    return EcsRequestResponse(
        400,
        {"code": 400, "message": "INVALID_ENVELOPE", "error_detail": {"issues": issues}},
        "invalid",
    )


def _placeholder() -> dict:
    return {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}


def _snapshot(binding: DeviceBinding) -> dict:
    return {
        "owner_domain": binding.business_code,
        "component_key": None,
        "work_line_id": binding.work_line_id,
        "device_id": binding.device_id,
        "device_role": binding.device_role,
        "device_version": binding.device_version,
        "work_line_version": binding.work_line_version,
    }


def _binding_matches_snapshot(binding: DeviceBinding, snapshot: dict) -> bool:
    return all(
        getattr(binding, field) == snapshot.get(field)
        for field in (
            "device_id",
            "device_version",
            "work_line_id",
            "work_line_version",
            "device_role",
        )
    )


def _resolve_registered(binding, request_type, resolve_input_target):
    registered = lookup(binding.device_role, request_type) if binding else None
    business_code = binding.business_code if binding is not None else None
    component_key = None
    shared = bool(
        registered
        and not registered.registration_source.local_only
        and registered.registration_source.module.startswith("app.businesses._components.")
    )
    if registered is not None and (
        shared
        or (
            resolve_input_target is not None
            and business_code is not None
            and registered.registration_source.module.startswith("app.businesses.")
            and not registered.registration_source.local_only
        )
    ):
        if resolve_input_target is None:
            raise RuntimeError("Shared ECS requests require a trusted input target resolver")
        target = resolve_input_target(
            business_code, ("ecs_request", request_type, binding.device_role)
        )
        if target is None or target.source != registered.registration_source:
            registered = None
        else:
            component_key = target.component_key
    return registered, business_code, component_key


async def _persist_decision(
    session: AsyncSession,
    *,
    receipt_id: int,
    operation_id: str,
    response: BaseModel,
    attempt_id: str,
    received_at: datetime,
) -> EcsRequestResponse:
    body = {"code": 200, "message": "DECIDED", "data": response.model_dump(mode="json")}
    prepared = history.encode_reply(body)
    current = await find_receipt_state(session, OPERATION, operation_id, for_update=True)
    if current is None:
        raise RuntimeError("request receipt disappeared before decision")
    if current.prepared_body is not None:
        if current.prepared_body != prepared:
            return EcsRequestResponse(
                current.status_code,
                current.body,
                "conflict",
                prepared_body=current.prepared_body,
                exchange_id=current.exchange_id,
                receipt_id=current.id,
                request_type=(current.event_envelope or {}).get("request_type"),
                device_code=current.device_code,
                digest=current.operation_id,
            )
        return EcsRequestResponse(
            current.status_code,
            current.body,
            "duplicate",
            prepared_body=current.prepared_body,
            exchange_id=current.exchange_id,
            receipt_id=current.id,
            request_type=(current.event_envelope or {}).get("request_type"),
            device_code=current.device_code,
            digest=current.operation_id,
        )
    exchange_id = await save_receipt_response(
        session,
        receipt_id=receipt_id,
        status_code=200,
        prepared_body=prepared,
        resp_timestamp=unix_ms(received_at),
        attempt_id=attempt_id,
        received_at=received_at,
    )
    return EcsRequestResponse(
        200,
        body,
        "decided",
        prepared_body=prepared,
        exchange_id=exchange_id,
        receipt_id=receipt_id,
        request_type=(current.event_envelope or {}).get("request_type"),
        device_code=current.device_code,
        digest=operation_id,
    )


async def _persist_deferred(session: AsyncSession, *, receipt_id: int, deferred: DeferredRequest):
    message = await session.get(ReliableMessage, deferred.message_id)
    if (
        message is None
        or message.operation != deferred.operation
        or message.operation_id != deferred.operation_id
    ):
        raise ValueError("deferred message identity is not visible in the current transaction")
    table = receipt_model()
    current = await session.scalar(
        select(table.event_context).where(active_receipt(table), table.receipt_id == receipt_id)
    )
    context = dict(current or {})
    context["deferred_request"] = {
        "message_id": deferred.message_id,
        "operation": deferred.operation,
        "operation_id": deferred.operation_id,
    }
    await session.execute(
        update(table)
        .where(active_receipt(table), table.receipt_id == receipt_id)
        .values(event_context=context)
    )


async def _run_handler(
    session: AsyncSession,
    *,
    registered: Registered,
    receipt_id: int,
    identity: str,
    envelope: EcsRequestEnvelope,
    binding: DeviceBinding,
    runtime: object | None,
    deferred: DeferredRequest | None,
    component_key: str | None,
):
    data = envelope.data
    try:
        data = registered.data_model.model_validate(data)
    except ValidationError as error:
        return _invalid(issues_from(error, model=registered.data_model, prefix="data"))
    ctx = EcsRequestContext(
        session=session,
        receipt_id=receipt_id,
        digest=identity,
        device_code=envelope.device_code,
        request_type=envelope.request_type,
        timestamp=envelope.timestamp,
        binding=binding,
        response_model=registered.response_model,
        deferred=deferred,
        runtime=runtime,
        business_code=binding.business_code,
        component_key=component_key,
    )
    await registered.func(ctx, data)
    if ctx.response is not None:
        return await _persist_decision(
            session,
            receipt_id=receipt_id,
            operation_id=identity,
            response=ctx.response,
            attempt_id=str(uuid7()),
            received_at=datetime.now(UTC),
        )
    if ctx.deferred is not None:
        await _persist_deferred(session, receipt_id=receipt_id, deferred=ctx.deferred)
        return EcsRequestResponse(
            503,
            _placeholder(),
            "deferred",
            receipt_id=receipt_id,
            request_type=envelope.request_type,
            device_code=envelope.device_code,
            digest=identity,
            deferred=True,
        )
    raise RequestHandlerIncomplete("request handler did not respond or defer")


async def accept_ecs_request(
    session: AsyncSession,
    raw: bytes,
    *,
    resolve_binding: BindingResolver | None = None,
    authorize_request: Callable[
        [AsyncSession, EcsRequestEnvelope, DeviceBinding | None], Awaitable[bool]
    ]
    | None = None,
    runtime: object | None = None,
    resolve_input_target=None,
    on_committed=None,
    on_business_committed=None,
    attempt_id: str | None = None,
) -> EcsRequestResponse:
    attempt_id = attempt_id or str(uuid7())
    if len(raw) > MAX_BODY_BYTES:
        return EcsRequestResponse(413, {"code": 413, "message": "PAYLOAD_TOO_LARGE"}, "too_large")
    parsed = json_object(raw)
    if parsed is None:
        return _invalid([{"field": "$", "code": "INVALID_JSON"}])
    try:
        envelope = EcsRequestEnvelope.model_validate(parsed)
    except ValidationError as error:
        return _invalid(issues_from(error, model=EcsRequestEnvelope))
    try:
        identity = request_identity(envelope)
        raw_digest = canonical_digest(parsed)
    except (ValueError, RecursionError):
        return _invalid([{"field": "$", "code": "INVALID_JSON"}])
    binding = await resolve_binding(session, envelope.device_code) if resolve_binding else None
    if binding is not None and binding.device_code != envelope.device_code:
        raise RuntimeError("ECS binding does not match request device")
    if authorize_request is not None and not await authorize_request(session, envelope, binding):
        return EcsRequestResponse(403, {"code": 403, "message": "FORBIDDEN_SOURCE"}, "forbidden")
    registered, business_code, component_key = _resolve_registered(
        binding, envelope.request_type, resolve_input_target
    )
    if registered is None:
        return EcsRequestResponse(400, {"code": 400, "message": "UNKNOWN_REQUEST"}, "unknown")
    existing = await find_receipt_state(session, OPERATION, identity)
    if existing is not None:
        if existing.prepared_body is not None:
            return EcsRequestResponse(
                existing.status_code,
                existing.body,
                "duplicate",
                prepared_body=existing.prepared_body,
                exchange_id=existing.exchange_id,
                receipt_id=existing.id,
                request_type=envelope.request_type,
                device_code=envelope.device_code,
                digest=identity,
            )
        deferred_data = (existing.event_context or {}).get("deferred_request")
        deferred = DeferredRequest(**deferred_data) if deferred_data else None
        return await _run_handler(
            session,
            registered=registered,
            receipt_id=existing.id,
            identity=identity,
            envelope=envelope,
            binding=binding,
            runtime=runtime,
            deferred=deferred,
            component_key=(existing.event_context or {}).get("component_key"),
        )
    receipt_id = await insert_receipt(
        session,
        operation=OPERATION,
        operation_id=identity,
        device_code=envelope.device_code,
        raw_digest=raw_digest,
        effective_digest=None,
        compare_mode="RAW",
        status_code=503,
        body=_placeholder(),
        resp_timestamp=unix_ms(datetime.now(UTC)),
        created_at=datetime.now(UTC),
        raw_body=raw,
        attempt_id=attempt_id,
        peer_system="ECS",
        protocol=PROTOCOL,
        interaction_kind="REQUEST",
        defer_response=True,
    )
    if receipt_id is None:
        existing = await find_receipt_state(session, OPERATION, identity)
        if existing is None:
            raise RuntimeError("request receipt identity conflict without a visible receipt")
        if existing.prepared_body is not None:
            return EcsRequestResponse(
                existing.status_code,
                existing.body,
                "duplicate",
                prepared_body=existing.prepared_body,
                receipt_id=existing.id,
                exchange_id=existing.exchange_id,
            )
        deferred_data = (existing.event_context or {}).get("deferred_request")
        deferred = DeferredRequest(**deferred_data) if deferred_data else None
        return await _run_handler(
            session,
            registered=registered,
            receipt_id=existing.id,
            identity=identity,
            envelope=envelope,
            binding=binding,
            runtime=runtime,
            deferred=deferred,
            component_key=component_key,
        )
    table = receipt_model()
    await session.execute(
        update(table)
        .where(active_receipt(table), table.receipt_id == receipt_id)
        .values(
            event_envelope=_request(envelope),
            event_context={**_snapshot(binding), "component_key": component_key},
            owner_domain=business_code,
        )
    )
    response = await _run_handler(
        session,
        registered=registered,
        receipt_id=receipt_id,
        identity=identity,
        envelope=envelope,
        binding=binding,
        runtime=runtime,
        deferred=None,
        component_key=component_key,
    )
    if (
        registered.registration_source.module.startswith("app.businesses._components.")
        and on_business_committed
    ):
        after_commit(session, partial(on_business_committed, binding.business_code))
    elif on_committed:
        after_commit(session, partial(on_committed, registered.registration_source))
    return response


async def continue_ecs_request(
    session: AsyncSession,
    *,
    identity: str,
    deferred: DeferredRequest,
    resolve_binding: BindingResolver | None = None,
    runtime: object | None = None,
    resolve_input_target=None,
) -> EcsRequestResponse:
    state = await find_receipt_state(session, OPERATION, identity, for_update=True)
    if state is None or state.prepared_body is not None:
        if state is None:
            raise RuntimeError("deferred request receipt disappeared")
        return EcsRequestResponse(
            state.status_code,
            state.body,
            "duplicate",
            prepared_body=state.prepared_body,
            receipt_id=state.id,
            exchange_id=state.exchange_id,
        )
    envelope = EcsRequestEnvelope.model_validate(state.event_envelope)
    binding = await resolve_binding(session, envelope.device_code) if resolve_binding else None
    if binding is None or not _binding_matches_snapshot(binding, state.event_context or {}):
        raise RequestHandlerIncomplete("deferred request binding changed after acceptance")
    registered, _, component_key = _resolve_registered(
        binding, envelope.request_type, resolve_input_target
    )
    if registered is None:
        raise RequestHandlerIncomplete("deferred request handler is no longer registered")
    response = await _run_handler(
        session,
        registered=registered,
        receipt_id=state.id,
        identity=identity,
        envelope=envelope,
        binding=binding,
        runtime=runtime,
        deferred=deferred,
        component_key=component_key,
    )
    if response.deferred:
        return response
    return response
