"""Compatibility queries for one exchange and its request and response sides."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Text,
    Uuid,
    column,
    exists,
    false,
    or_,
    select,
    table,
)

from app.core.errors import AppError, ErrorCode
from app.database import SCHEMA
from app.models import RuntimeRetentionHold, TransportTask
from app.schemas.integration_history import HttpInitiator
from app.schemas.query import parse_filter
from app.services import integration_history_service as legacy
from app.services.filtering import FieldSpec, apply_filter

SUMMARY = (
    "id",
    "schema_version",
    "role",
    "peer_system",
    "protocol",
    "initiator",
    "interaction_kind",
    "operation",
    "operation_id",
    "message_type",
    "identity_kind",
    "identity_value",
    "owner_domain",
    "device_code",
    "created_at",
    "recorded_at",
    "capture_origin",
    "coverage",
    "trace_id",
    "reason_code",
    "failure_kind",
    "close_reason",
    "late_response_at",
)
SUMMARY_EXTRA = (
    "delivery_state",
    "delivery_uncertain",
    "protocol_code",
    "protocol_verdict",
    "protocol_http_status",
    "response_scope",
    "protocol_source_record_id",
    "application_state",
    "outcome_revision",
    "needs_reconciliation",
    "command_code",
    "receipt_id",
    "receipt_active",
    "request_record_id",
    "response_record_id",
    "ack_saved",
    "command_matched",
    "message_id",
    "transport_task_id",
    "task_match_state",
    "pending_result_receipt_id",
    "applied_result_receipt_id",
    "applied_result_digest",
    "applied_trigger_receipt_id",
    "superseded_by_message_id",
    "replayed_from_id",
    "attempt_state",
)
RECORDS = (
    *(name for name in legacy.RECORD_FIELDS if name != "direction"),
    "initiator",
    "body_state",
    "body_sha256",
    "stored_bytes",
    "side",
)
TYPES = {
    **dict.fromkeys(
        (
            "id",
            "exchange_id",
            "parent_record_id",
            "body_record_id",
            "request_record_id",
            "response_record_id",
            "protocol_source_record_id",
            "replayed_from_id",
        ),
        Uuid,
    ),
    **dict.fromkeys(
        ("created_at", "recorded_at", "occurred_at", "late_response_at"), DateTime(timezone=True)
    ),
    **dict.fromkeys(("schema_version", "http_status", "protocol_http_status"), Integer),
    **dict.fromkeys(
        (
            "message_id",
            "receipt_id",
            "stored_bytes",
            "outcome_revision",
            "pending_result_receipt_id",
            "applied_result_receipt_id",
            "applied_trigger_receipt_id",
            "superseded_by_message_id",
        ),
        BigInteger,
    ),
    **dict.fromkeys(
        (
            "delivery_uncertain",
            "receipt_active",
            "needs_reconciliation",
            "late",
            "ack_saved",
            "command_matched",
        ),
        Boolean,
    ),
}
EXCHANGE = table(
    "integration_exchange_summary",
    *(column(name, TYPES.get(name, Text)) for name in (*SUMMARY, *SUMMARY_EXTRA)),
    schema=SCHEMA,
)
RECORD = table(
    "integration_record_summary",
    *(
        column(name, TYPES.get(name, Text))
        for name in (*RECORDS, "peer_system", "trace_id", "reason_code")
    ),
    schema=SCHEMA,
)


def _summary(row):
    item = {name: row[name] for name in SUMMARY}
    for name in ("created_at", "recorded_at", "late_response_at"):
        if item[name] is not None:
            item[name] = legacy._utc(item[name])
    item["delivery"] = {
        "state": row["delivery_state"],
        "uncertain": row["delivery_uncertain"],
        "attempt_state": row["attempt_state"],
        "message_id": row["message_id"],
        "superseded_by_message_id": row["superseded_by_message_id"],
    }
    item["protocol_result"] = {
        "code": row["protocol_code"],
        "verdict": row["protocol_verdict"],
        "http_status": row["protocol_http_status"],
        "response_scope": row["response_scope"],
        "ack_received": row["ack_saved"] if row["command_code"] else None,
        "source_record_id": row["protocol_source_record_id"],
    }
    item["application"] = {
        "state": row["application_state"],
        "outcome_revision": row["outcome_revision"],
        "needs_reconciliation": row["needs_reconciliation"],
        "pending_result_receipt_id": row["pending_result_receipt_id"],
        "applied_result_receipt_id": row["applied_result_receipt_id"],
        "applied_result_digest": row["applied_result_digest"],
        "applied_trigger_receipt_id": row["applied_trigger_receipt_id"],
    }
    return item


def _hold_subjects():
    h = EXCHANGE.c
    return or_(
        (RuntimeRetentionHold.subject_kind == "RECEIPT")
        & (RuntimeRetentionHold.subject_id == h.receipt_id.cast(Text)),
        (RuntimeRetentionHold.subject_kind == "WMS_MESSAGE")
        & (RuntimeRetentionHold.subject_id == h.message_id.cast(Text)),
        (RuntimeRetentionHold.subject_kind == "ECS_COMMAND")
        & (RuntimeRetentionHold.subject_id == h.command_code)
        & h.command_matched.is_(True),
        (RuntimeRetentionHold.subject_kind == "TRANSPORT_TASK")
        & (RuntimeRetentionHold.subject_id == h.transport_task_id)
        & (h.task_match_state == "MATCHED"),
    )


def _reference(kind, namespace, value):
    h = EXCHANGE.c
    if (kind, namespace) == ("RELIABLE_MESSAGE", "id"):
        return h.message_id.cast(Text) == value
    if (kind, namespace) == ("TRANSPORT_TASK", "id"):
        return (h.transport_task_id == value) & (h.task_match_state == "MATCHED")
    if (kind, namespace) == ("EXCHANGE", "integration"):
        try:
            identity = UUID(value)
        except ValueError:
            return false()
        original = EXCHANGE.alias("replay_request")
        return or_(
            h.id == identity,
            h.replayed_from_id == identity,
            h.id
            == select(original.c.replayed_from_id)
            .where(original.c.id == identity)
            .scalar_subquery(),
        )
    if (kind, namespace) == ("WORKLINE", "code"):
        return (h.task_match_state == "MATCHED") & exists().where(
            TransportTask.transport_task_id == h.transport_task_id,
            TransportTask.workline_id == value,
        )
    if (kind, namespace) == ("WORKLINE", "id"):
        from app.models import CallbackReceipt

        return exists().where(
            CallbackReceipt.receipt_id == h.receipt_id,
            CallbackReceipt.event_context["work_line_id"].as_integer().cast(Text) == value,
        )
    if kind in ("BUSINESS_TASK", "BUSINESS_PREPARATION", "BUSINESS_ACTION"):
        return exists().where(
            _hold_subjects(),
            RuntimeRetentionHold.owner_kind == kind,
            RuntimeRetentionHold.owner_domain == namespace,
            RuntimeRetentionHold.owner_id == value,
        )
    return false()


async def list_exchanges(session, params):
    if params.direction is not None:
        raise legacy._invalid()
    h = EXCHANGE.c
    statement = select(EXCHANGE)
    for name in (
        "peer_system",
        "initiator",
        "operation",
        "device_code",
        "owner_domain",
        "identity_kind",
        "identity_value",
    ):
        if (value := getattr(params, name)) is not None:
            statement = statement.where(h[name] == value)
    reference = (params.reference_kind, params.reference_namespace, params.reference_value)
    if any(value is not None for value in reference):
        if any(value is None for value in reference):
            raise legacy._invalid()
        statement = statement.where(_reference(*reference))
    fields = {
        name: FieldSpec(h[name], HttpInitiator if name == "initiator" else str, ("eq", "in"))
        for name in (
            "peer_system",
            "protocol",
            "initiator",
            "operation",
            "device_code",
            "owner_domain",
            "role",
            "coverage",
        )
    }
    statement = apply_filter(statement, parse_filter(params.filter), fields)
    rows, cursor = await legacy._page(
        session,
        statement,
        params=params,
        at=h.created_at,
        identity=h.id,
        endpoint="http-exchanges",
        exact=params.identity_value is not None
        or (reference[0] is not None and reference[0] != "WORKLINE"),
    )
    return {"items": [_summary(row) for row in rows], "next_cursor": cursor}


async def get_exchange(session, exchange_id):
    h = EXCHANGE.c
    row = (
        (await session.execute(select(EXCHANGE).where(h.id == exchange_id)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "HTTP 往来不存在")
    references = []
    for kind, value in (
        ("RELIABLE_MESSAGE", row["message_id"]),
        ("CALLBACK_RECEIPT", row["receipt_id"]),
        (
            "TRANSPORT_TASK",
            row["transport_task_id"] if row["task_match_state"] == "MATCHED" else None,
        ),
    ):
        if value is not None:
            references.append(
                dict(
                    exchange_id=exchange_id,
                    reference_kind=kind,
                    namespace="id",
                    reference_value=str(value),
                    relation_role="ORIGINAL",
                    source_record_id=None,
                    related_exchange_id=None,
                )
            )
    if row["replayed_from_id"] is not None:
        references.append(
            dict(
                exchange_id=exchange_id,
                reference_kind="EXCHANGE",
                namespace="integration",
                reference_value=str(row["replayed_from_id"]),
                relation_role="REPLAYED_FROM",
                source_record_id=row["response_record_id"],
                related_exchange_id=row["replayed_from_id"],
            )
        )
    replays = (
        await session.execute(
            select(h.id, h.response_record_id)
            .where(h.replayed_from_id == exchange_id)
            .order_by(h.created_at, h.id)
            .limit(101)
        )
    ).all()
    references.extend(
        dict(
            exchange_id=replay.id,
            reference_kind="EXCHANGE",
            namespace="integration",
            reference_value=str(exchange_id),
            relation_role="REPLAYED_FROM",
            source_record_id=replay.response_record_id,
            related_exchange_id=exchange_id,
        )
        for replay in replays
    )
    owners = (
        await session.execute(
            select(
                RuntimeRetentionHold.owner_kind,
                RuntimeRetentionHold.owner_domain,
                RuntimeRetentionHold.owner_id,
            )
            .where(
                exists(select(1).select_from(EXCHANGE).where(h.id == exchange_id, _hold_subjects()))
            )
            .distinct()
            .order_by(
                RuntimeRetentionHold.owner_kind,
                RuntimeRetentionHold.owner_domain,
                RuntimeRetentionHold.owner_id,
            )
            .limit(101)
        )
    ).all()
    references.extend(
        dict(
            exchange_id=exchange_id,
            reference_kind=kind,
            namespace=domain,
            reference_value=value,
            relation_role="RETENTION",
            source_record_id=None,
            related_exchange_id=None,
        )
        for kind, domain, value in owners
    )
    return dict(
        _summary(row),
        command_code=row["command_code"],
        receipt_id=row["receipt_id"],
        receipt_active=row["receipt_active"],
        request_record_id=row["request_record_id"],
        ack_record_id=row["response_record_id"]
        if row["protocol_code"] == "ACK" and row["interaction_kind"] == "COMMAND"
        else None,
        current_response_record_id=row["response_record_id"],
        references=references[:100],
        references_truncated=len(references) > 100,
    )


async def list_records(session, exchange_id, params):
    h, r = EXCHANGE.c, RECORD.c
    if await session.scalar(select(h.id).where(h.id == exchange_id)) is None:
        raise AppError(ErrorCode.NOT_FOUND, "HTTP 往来不存在")
    statement = select(RECORD).where(r.exchange_id == exchange_id)
    for name in ("record_kind", "evidence_class"):
        if (value := getattr(params, name)) is not None:
            statement = statement.where(r[name] == value)
    statement = apply_filter(
        statement,
        parse_filter(params.filter),
        {
            name: FieldSpec(r[name], HttpInitiator if name == "initiator" else str, ("eq", "in"))
            for name in ("record_kind", "evidence_class", "initiator", "capture_origin")
        },
    )
    rows, cursor = await legacy._page(
        session,
        statement,
        params=params,
        at=r.recorded_at,
        identity=r.id,
        endpoint=f"http-exchange-records:{exchange_id}",
        exact=True,
    )
    items = []
    for row in rows:
        item = {name: row[name] for name in RECORDS}
        item.update(
            recorded_at=legacy._utc(row["recorded_at"]),
            occurred_at=legacy._utc(row["occurred_at"]),
            metadata={name: row[name] for name in ("trace_id", "reason_code") if row[name]} or None,
        )
        items.append(item)
    return {"items": items, "next_cursor": cursor}


async def locate_body(session, record_id):
    # Side IDs use the exchange UUID with a reserved version nibble.
    side = {4: "request", 5: "response"}.get(record_id.version)
    if side is None:
        raise AppError(ErrorCode.NOT_FOUND, "证据记录不存在")
    exchange_id = UUID(hex=record_id.hex[:12] + "7" + record_id.hex[13:])
    h = EXCHANGE.c
    peer = await session.scalar(
        select(h.peer_system).where(h.id == exchange_id, h[f"{side}_record_id"] == record_id)
    )
    if peer is None:
        raise AppError(ErrorCode.NOT_FOUND, "证据记录不存在")
    return exchange_id, peer, side
