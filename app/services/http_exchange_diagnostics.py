"""Read-only troubleshooting by original protocol identity."""

from sqlalchemy import select, tuple_

from app.core.errors import AppError, ErrorCode
from app.integrations.callback_receipts import receipt_model
from app.integrations.http_exchanges import operation_uuid, safe_text
from app.integrations.runtime_authority import enabled
from app.integrations.wms_request.results import get_result as wms_result
from app.integrations.wms_transport.results import get_result as transport_result
from app.integrations.wms_transport.types import TransportCaller
from app.models import (
    EcsCommand,
    EcsHttpExchange,
    IntegrationExchange,
    ReliableMessage,
    TransportHttpExchange,
    TransportTask,
    WmsHttpExchange,
)

EXCHANGES = {
    "WMS": (WmsHttpExchange, "operation_id"),
    "ECS": (EcsHttpExchange, "command_code"),
    "TRANSPORT": (TransportHttpExchange, "transport_task_id"),
}
SUMMARY = (
    "id",
    "direction",
    "interaction_kind",
    "traffic_source",
    "started_at",
    "finished_at",
    "http_method",
    "endpoint_path",
    "peer_endpoint",
    "trace_id",
    "attempt_state",
    "close_reason",
    "http_status",
    "protocol_code",
    "protocol_verdict",
    "reported_result",
    "handling_outcome",
    "reason_code",
    "failure_kind",
    "late_response_at",
    "replayed_from_id",
    "message_id",
    "receipt_id",
    "request_wire_state",
    "response_wire_state",
)


async def diagnostics(session, *, peer, reference, limit, before):
    model, key = EXCHANGES[peer]
    value = operation_uuid(reference) if peer == "WMS" else reference
    if value is None or safe_text(reference) is None:
        raise AppError(ErrorCode.INVALID_REQUEST, "无效的对接编号")
    match = getattr(model, key) == value
    extras = {
        "WMS": ("operation",),
        "ECS": ("device_code", "task_type", "event_type"),
        "TRANSPORT": ("task_match_state", "outcome_revision"),
    }
    names = (*SUMMARY, key, *extras[peer])
    query = select(*(getattr(model, name) for name in names)).where(match)
    if before is not None:
        anchor = (
            await session.execute(
                select(model.started_at, model.id).where(match, model.id == before)
            )
        ).one_or_none()
        if anchor is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "无效的往来分页位置")
        query = query.where(tuple_(model.started_at, model.id) < tuple_(*anchor))
    timeline = (
        (
            await session.execute(
                query.order_by(model.started_at.desc(), model.id.desc()).limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )
    runtime = []
    if peer == "WMS":
        message = ReliableMessage
        query = select(
            message.id,
            message.operation,
            message.target,
            message.pause_reason,
            message.dispatch_started_at,
            message.superseded_by_message_id,
        ).where(message.operation_id == reference)
        protocol = message.protocol
        if not enabled():
            query = query.join(IntegrationExchange, IntegrationExchange.id == message.exchange_id)
            protocol = IntegrationExchange.protocol
        rows = (
            await session.execute(
                query.where(protocol.in_(("wms-decisions", "wms-facts")))
                .order_by(message.id)
                .limit(101)
            )
        ).all()
        for item in rows[:100]:
            result = await wms_result(
                session, message_id=item.id, operation=item.operation, operation_id=reference
            )
            runtime.append(
                dict(
                    direction="OUTBOUND",
                    message_id=item.id,
                    operation=item.operation,
                    target=item.target,
                    delivery_status=result.delivery_status,
                    delivery_uncertain=result.delivery_uncertain,
                    dispatch_started_at=item.dispatch_started_at,
                    reason=item.pause_reason or result.fail_reason,
                    protocol_code=result.response_code,
                    consumption_state="UNKNOWN",
                    consumption_reason="BUSINESS_CONSUMPTION_NOT_CONFIRMED",
                    response_state=result.response_state,
                    superseded_by_message_id=item.superseded_by_message_id,
                )
            )
        receipt = receipt_model()
        active = receipt.active if enabled() else receipt.receipt_active
        query = select(
            receipt.receipt_id,
            receipt.operation,
            receipt.status_code,
            active.label("receipt_active"),
        )
        if not enabled():
            query = query.where(receipt.role == "RECEIPT")
        receipts = (
            (
                await session.execute(
                    query.where(
                        receipt.protocol == "wms-callback",
                        receipt.operation_id == reference,
                        ~receipt.operation.like("transport.task.%"),
                    )
                    .order_by(receipt.receipt_id)
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        runtime.extend(dict(direction="INBOUND", **row) for row in receipts[:100])
        runtime_truncated = len(rows) > 100 or len(receipts) > 100
    elif peer == "ECS":
        command = EcsCommand if enabled() else IntegrationExchange
        row = (
            (
                await session.execute(
                    select(
                        command.command_code,
                        command.status,
                        command.delivery_status,
                        command.dispatch_started_at,
                        command.hold_reason,
                        command.needs_reconciliation,
                        command.ack_record_id.is_not(None).label("ack_saved"),
                        command.applied_result_id.is_not(None).label("result_applied"),
                    ).where(
                        command.command_code == reference,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            runtime.append(dict(row))
        runtime_truncated = False
    else:
        task = (
            await session.execute(
                select(
                    TransportTask.client_request_id,
                    TransportTask.workline_id,
                    TransportTask.station_id,
                ).where(TransportTask.transport_task_id == reference)
            )
        ).one_or_none()
        if task is not None:
            result = await transport_result(
                session,
                transport_task_id=reference,
                client_request_id=task.client_request_id,
                caller=TransportCaller(task.workline_id, task.station_id),
            )
            runtime.append(
                dict(
                    transport_task_id=reference,
                    submission_state=result.submission_state,
                    delivery_status=result.submission_delivery_status,
                    pending_revision=result.pending_revision,
                    outcome_revision=result.outcome_revision,
                    pending_result_receipt_id=result.pending_result_receipt_id,
                    applied_result_receipt_id=result.applied_result_receipt_id,
                    applied_trigger_receipt_id=result.applied_trigger_receipt_id,
                    closure_reason=result.closure_reason,
                )
            )
        runtime_truncated = False
    return dict(
        items=[dict(row) for row in timeline[:limit]],
        runtime=runtime,
        runtime_truncated=runtime_truncated,
        next_before=timeline[limit - 1]["id"] if len(timeline) > limit else None,
    )
