"""Body-free history projections; execution remains with original public result ports."""

import base64
import binascii
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import case, or_, select, tuple_

from app.core.errors import AppError, ErrorCode
from app.integrations.ecs_callback import EventApplicationRef
from app.integrations.history import read_bodies
from app.models import IntegrationExchange as Exchange
from app.models import IntegrationRecord as Record
from app.models import IntegrationReference as Reference
from app.models import ReliableMessage, TransportTask
from app.schemas.query import parse_filter
from app.services.filtering import FieldSpec, apply_filter

EXCHANGE_FIELDS = (
    "id",
    "schema_version",
    "role",
    "peer_system",
    "protocol",
    "direction",
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
    "command_code",
    "receipt_id",
    "receipt_active",
    "request_record_id",
    "ack_record_id",
    "current_response_record_id",
    "status",
    "delivery_status",
    "status_code",
    "needs_reconciliation",
    "applied_result_id",
    "task_type",
    "application_ref",
)
RECORD_FIELDS = (
    "id",
    "exchange_id",
    "record_kind",
    "evidence_class",
    "attempt_id",
    "parent_record_id",
    "direction",
    "occurred_at",
    "recorded_at",
    "capture_origin",
    "http_status",
    "network_state",
    "protocol_verdict",
    "protocol_code",
    "business_result",
    "application_state",
    "late",
    "outcome_revision",
    "body_record_id",
    "source_kind",
    "source_key",
    "source_version",
    "response_record_id",
)
SAFE_METADATA = {
    "purged_at",
    "attempt_count",
    "recorded_delivery_state",
    "source",
    "request_id",
    "trace_id",
    "truncated",
    "coverage_reason",
    "response_scope",
    "owner_domain",
    "action_id",
    "source_kind",
    "source_identity",
    "source_revision",
    "trigger_record_id",
    "outcome",
    "projection_state",
}


def utc_now():
    return datetime.now(UTC)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stamp(value):
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _invalid():
    return AppError(ErrorCode.INVALID_REQUEST, "历史查询参数无效")


def _encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode(raw, query_hash):
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 256:
            raise ValueError
        data = json.loads(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(data, dict)
            or set(data) not in ({"v", "at", "id", "q"}, {"v", "at", "id", "q", "u"})
            or type(data["v"]) is not int
            or data["v"] != 1
            or data["q"] != query_hash
            or _encode(data) != raw
        ):
            raise ValueError
        for name in ("at", "u"):
            if name in data:
                if (
                    not isinstance(data[name], str)
                    or _stamp(datetime.fromisoformat(data[name])) != data[name]
                ):
                    raise ValueError
        if not isinstance(data["id"], str) or list(data) != ["v", "at", "id", "q"] + (
            ["u"] if "u" in data else []
        ):
            raise ValueError
        identity = UUID(data["id"])
        if str(identity) != data["id"]:
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError):
        raise _invalid() from None


async def _page(session, statement, *, params, at, identity, endpoint, exact):
    explicit = params.from_at is not None or params.until_at is not None
    if explicit and (params.from_at is None or params.until_at is None):
        raise _invalid()
    if explicit and not timedelta(0) < params.until_at - params.from_at <= timedelta(days=90):
        raise _invalid()
    query = params.model_dump(mode="json", exclude={"cursor", "limit"})
    for name in ("from_at", "until_at"):
        if getattr(params, name) is not None:
            query[name] = _stamp(getattr(params, name))
    # A new optional filter must not invalidate existing unfiltered cursors.
    if query.get("initiator") is None:
        query.pop("initiator", None)
    query["filter"] = parse_filter(params.filter).model_dump(mode="json") if params.filter else None
    digest = sha256(
        json.dumps([endpoint, query], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    cursor = _decode(params.cursor, digest) if params.cursor is not None else None
    default_window = not explicit and not exact
    if cursor and (("u" in cursor) != default_window):
        raise _invalid()
    until = datetime.fromisoformat(cursor["u"]) if cursor and default_window else utc_now()
    if explicit:
        statement = statement.where(at >= params.from_at, at <= params.until_at)
    elif default_window:
        statement = statement.where(at >= until - timedelta(days=90), at <= until)
    if cursor:
        boundary = datetime.fromisoformat(cursor["at"])
        if default_window and not until - timedelta(days=90) <= boundary <= until:
            raise _invalid()
        if explicit and not params.from_at <= boundary <= params.until_at:
            raise _invalid()
        statement = statement.where(tuple_(at, identity) < tuple_(boundary, UUID(cursor["id"])))
    with session.no_autoflush:
        rows = list(
            (
                await session.execute(
                    statement.order_by(at.desc(), identity.desc()).limit(params.limit + 1)
                )
            ).mappings()
        )
    next_cursor = None
    if len(rows) > params.limit:
        last = rows[params.limit - 1]
        value = dict(v=1, at=_stamp(last[at.key]), id=str(last["id"]), q=digest)
        if default_window:
            value["u"] = _stamp(until)
        next_cursor = _encode(value)
        if len(next_cursor) > 256:
            raise _invalid()
    return rows[: params.limit], next_cursor


def _exchange_statement():
    return select(*(getattr(Exchange, name) for name in EXCHANGE_FIELDS))


async def _summaries(session, rows):
    if not rows:
        return []
    ids = tuple(row["id"] for row in rows)
    with session.no_autoflush:
        messages = list(
            (
                await session.execute(
                    select(
                        ReliableMessage.exchange_id,
                        ReliableMessage.status,
                        ReliableMessage.delivery_uncertain,
                        ReliableMessage.last_verdict,
                        ReliableMessage.last_http_status,
                        ReliableMessage.last_response_record_id,
                        ReliableMessage.terminal_response_record_id,
                    ).where(ReliableMessage.exchange_id.in_(ids))
                )
            ).mappings()
        )
        tasks = list(
            (
                await session.execute(
                    select(
                        TransportTask.submission_exchange_id,
                        TransportTask.outcome_status,
                        TransportTask.applied_outcome_revision,
                        TransportTask.transport_task_id,
                    ).where(TransportTask.submission_exchange_id.in_(ids))
                )
            ).mappings()
        )
        facts = list(
            (
                await session.execute(
                    select(
                        Record.id,
                        Record.exchange_id,
                        Record.record_kind,
                        Record.semantic_digest,
                        Record.outcome_revision,
                        Record.applied,
                        Record.protocol_code,
                    ).where(
                        Record.exchange_id.in_(ids),
                        Record.evidence_class == "CORE",
                        or_(
                            (Record.record_kind == "RESULT_EVIDENCE")
                            & tuple_(Record.exchange_id, Record.semantic_digest).in_(
                                [
                                    (row["id"], row["applied_result_id"])
                                    for row in rows
                                    if row["applied_result_id"] is not None
                                ]
                            ),
                            (Record.record_kind == "RESULT_APPLIED")
                            & tuple_(Record.exchange_id, Record.outcome_revision).in_(
                                [
                                    (
                                        task["submission_exchange_id"],
                                        task["applied_outcome_revision"],
                                    )
                                    for task in tasks
                                    if task["applied_outcome_revision"] is not None
                                ]
                            ),
                            Record.id.in_(
                                [
                                    row["current_response_record_id"]
                                    for row in rows
                                    if row["current_response_record_id"] is not None
                                ]
                            ),
                        ),
                    )
                )
            ).mappings()
        )
    output = []
    private = {
        "command_code",
        "receipt_id",
        "receipt_active",
        "request_record_id",
        "ack_record_id",
        "current_response_record_id",
        "status",
        "delivery_status",
        "status_code",
        "needs_reconciliation",
        "applied_result_id",
        "task_type",
        "application_ref",
    }
    for row in rows:
        item = {key: value for key, value in row.items() if key not in private}
        item["created_at"], item["recorded_at"] = (
            _utc(item["created_at"]),
            _utc(item["recorded_at"]),
        )
        item["message_type"] = item["message_type"] or row["task_type"]
        originals = [m for m in messages if m["exchange_id"] == row["id"]]
        task_rows = [t for t in tasks if t["submission_exchange_id"] == row["id"]]
        message = originals[0] if len(originals) == 1 else None
        task = task_rows[0] if len(task_rows) == 1 else None
        item["delivery"] = {
            "state": row["delivery_status"] or (message["status"] if message else "UNKNOWN"),
            "uncertain": message["delivery_uncertain"] if message else None,
        }
        reply = next(
            (fact for fact in facts if fact["id"] == row["current_response_record_id"]), None
        )
        item["protocol_result"] = {
            "code": reply["protocol_code"] if reply else None,
            "verdict": message["last_verdict"] if message else None,
            "http_status": row["status_code"]
            if row["current_response_record_id"]
            else (message["last_http_status"] if message else None),
            "response_scope": "PROTOCOL_PREPARED"
            if row["current_response_record_id"]
            else "PEER_OBSERVATION",
            "ack_received": row["ack_record_id"] is not None
            if row["role"] == "ECS_COMMAND"
            else None,
            "source_record_id": row["current_response_record_id"]
            or (
                message["last_response_record_id"] or message["terminal_response_record_id"]
                if message
                else None
            )
            or row["ack_record_id"],
        }
        item["application"] = {
            "state": row["status"]
            or (task["outcome_status"] if task and task["outcome_status"] else "UNKNOWN"),
            "outcome_revision": task["applied_outcome_revision"] if task else None,
            "needs_reconciliation": row["needs_reconciliation"],
        }
        applied = None
        if row["applied_result_id"] is not None:
            applied = next(
                (
                    fact
                    for fact in facts
                    if fact["exchange_id"] == row["id"]
                    and fact["record_kind"] == "RESULT_EVIDENCE"
                    and fact["applied"] is True
                    and fact["semantic_digest"] == row["applied_result_id"]
                ),
                None,
            )
        elif task and task["applied_outcome_revision"] is not None:
            applied = next(
                (
                    fact
                    for fact in facts
                    if fact["exchange_id"] == row["id"]
                    and fact["record_kind"] == "RESULT_APPLIED"
                    and fact["outcome_revision"] == task["applied_outcome_revision"]
                ),
                None,
            )
        item["application"]["source_record_id"] = applied["id"] if applied else None
        if applied is None and (
            row["status"] in {"SUCCEEDED", "FAILED"}
            or (task and task["applied_outcome_revision"] is not None)
        ):
            item["application"].update(
                recorded_state=item["application"]["state"],
                state="UNKNOWN",
                evidence_reason="APPLIED_EVIDENCE_UNAVAILABLE",
            )
        if row.get("application_ref"):
            try:
                ref = EventApplicationRef(**row["application_ref"])
            except (ValueError, TypeError):
                item["application"].update(
                    state="UNKNOWN", evidence_reason="INVALID_APPLICATION_REFERENCE"
                )
            else:
                item["application"].update(state="RECORDED_APPLICATION", reference=asdict(ref))
        output.append(item)
    return output


async def list_exchanges(session, params):
    if params.initiator is not None:
        raise _invalid()
    statement = _exchange_statement()
    for name in (
        "peer_system",
        "direction",
        "operation",
        "device_code",
        "owner_domain",
        "identity_kind",
        "identity_value",
    ):
        value = getattr(params, name)
        if value is not None:
            statement = statement.where(getattr(Exchange, name) == value)
    reference = (params.reference_kind, params.reference_namespace, params.reference_value)
    if any(v is not None for v in reference):
        if any(v is None for v in reference):
            raise _invalid()
        matched = (
            select(Reference.exchange_id)
            .where(
                Reference.reference_kind == reference[0],
                Reference.namespace == reference[1],
                Reference.value_digest == sha256(reference[2].encode()).hexdigest(),
                Reference.reference_value == reference[2],
            )
            .cte("matching_references")
        )
        original = select(matched.c.exchange_id)
        edge = Reference.__table__.alias("history_edge")
        neighbors = (
            select(edge.c.exchange_id)
            .where(edge.c.reference_kind == "EXCHANGE", edge.c.related_exchange_id.in_(original))
            .union(
                select(edge.c.related_exchange_id).where(
                    edge.c.reference_kind == "EXCHANGE", edge.c.exchange_id.in_(original)
                )
            )
        )
        statement = statement.where(or_(Exchange.id.in_(original), Exchange.id.in_(neighbors)))
    fields = {
        name: FieldSpec(getattr(Exchange, name), str, ("eq", "in"))
        for name in (
            "peer_system",
            "protocol",
            "direction",
            "operation",
            "device_code",
            "owner_domain",
            "role",
            "coverage",
        )
    }
    statement = apply_filter(statement, parse_filter(params.filter), fields)
    rows, cursor = await _page(
        session,
        statement,
        params=params,
        at=Exchange.created_at,
        identity=Exchange.id,
        endpoint="exchanges",
        exact=params.identity_value is not None
        or (
            params.reference_value is not None
            and params.reference_kind
            in {
                "RELIABLE_MESSAGE",
                "TRANSPORT_TASK",
                "BUSINESS_TASK",
                "BUSINESS_PREPARATION",
                "BUSINESS_ACTION",
                "EXCHANGE",
            }
        ),
    )
    return {"items": await _summaries(session, rows), "next_cursor": cursor}


async def get_exchange(session, exchange_id):
    with session.no_autoflush:
        row = (
            (await session.execute(_exchange_statement().where(Exchange.id == exchange_id)))
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "交互记录不存在")
    (result,) = await _summaries(session, (row,))
    result.update(
        {
            key: row[key]
            for key in (
                "command_code",
                "receipt_id",
                "receipt_active",
                "request_record_id",
                "ack_record_id",
                "current_response_record_id",
            )
        }
    )
    with session.no_autoflush:
        refs = list(
            (
                await session.execute(
                    select(
                        Reference.exchange_id,
                        Reference.reference_kind,
                        Reference.namespace,
                        Reference.reference_value,
                        Reference.relation_role,
                        Reference.source_record_id,
                        Reference.related_exchange_id,
                    )
                    .where(
                        or_(
                            Reference.exchange_id == exchange_id,
                            Reference.related_exchange_id == exchange_id,
                        )
                    )
                    .order_by(Reference.created_at, Reference.id)
                    .limit(101)
                )
            ).mappings()
        )
    result["references"] = [dict(ref) for ref in refs[:100]]
    result["references_truncated"] = len(refs) > 100
    return result


async def list_records(session, exchange_id, params):
    with session.no_autoflush:
        if await session.scalar(select(Exchange.id).where(Exchange.id == exchange_id)) is None:
            raise AppError(ErrorCode.NOT_FOUND, "交互记录不存在")
    body = Record.__table__.alias("canonical_body")
    statement = (
        select(
            *(getattr(Record, name) for name in RECORD_FIELDS),
            Record.details.label("metadata"),
            *(
                case(
                    (Record.body_record_id.is_not(None), body.c[name]), else_=getattr(Record, name)
                ).label(name)
                for name in ("body_state", "body_sha256", "stored_bytes")
            ),
        )
        .outerjoin(body, body.c.id == Record.body_record_id)
        .where(Record.exchange_id == exchange_id)
    )
    if params.record_kind:
        statement = statement.where(Record.record_kind == params.record_kind)
    if params.evidence_class:
        statement = statement.where(Record.evidence_class == params.evidence_class)
    fields = {
        name: FieldSpec(getattr(Record, name), str, ("eq", "in"))
        for name in ("record_kind", "evidence_class", "direction", "capture_origin")
    }
    statement = apply_filter(statement, parse_filter(params.filter), fields)
    rows, cursor = await _page(
        session,
        statement,
        params=params,
        at=Record.recorded_at,
        identity=Record.id,
        endpoint="records:" + str(exchange_id),
        exact=True,
    )
    items = []
    for row in rows:
        item = dict(row)
        for name in ("occurred_at", "recorded_at"):
            if item[name] is not None:
                item[name] = _utc(item[name])
        item["body_state"] = item["body_state"] or "UNAVAILABLE"
        item["metadata"] = {
            key: value for key, value in (item["metadata"] or {}).items() if key in SAFE_METADATA
        } or None
        if item["record_kind"] == "RESPONSE_PREPARED":
            item["metadata"] = {"response_scope": "PROTOCOL_PREPARED", **(item["metadata"] or {})}
        items.append(item)
    return {"items": items, "next_cursor": cursor}


async def get_body(session, record_id):
    bodies = await read_bodies(session, record_ids=(record_id,))
    stored = bodies.get(record_id)
    if stored is None:
        raise AppError(ErrorCode.NOT_FOUND, "证据记录不存在")
    if stored.raw_body is None:
        raise AppError(ErrorCode.NOT_FOUND, "正文不可用", detail=stored.body_state)
    return stored
