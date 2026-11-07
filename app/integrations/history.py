"""Integration facts use their caller's transaction; reads never repair storage."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import aliased

from app.core.identifiers import uuid7
from app.models import IntegrationExchange, IntegrationRecord, IntegrationReference
from app.reliable.lifecycle import (
    EvidenceIntegrityError,
    EvidencePersistenceError,
    ensure_savepoint_transaction,
)


@dataclass(frozen=True, kw_only=True)
class RecordDraft:
    exchange_id: UUID
    record_kind: str
    event_key: str
    direction: str
    evidence_class: str = "CORE"
    attempt_id: str | None = None
    parent_record_id: UUID | None = None
    occurred_at: datetime | None = None
    capture_origin: str = "LIVE"
    http_status: int | None = None
    network_state: str | None = None
    protocol_verdict: str | None = None
    protocol_code: str | None = None
    business_result: str | None = None
    application_state: str | None = None
    late: bool | None = None
    outcome_revision: int | None = None
    raw_body: bytes | None = None
    body_record_id: UUID | None = None
    body_state: str | None = None
    details: dict | None = None
    source_kind: str | None = None
    source_key: str | None = None
    source_version: str | None = None
    semantic_digest: str | None = None
    effective_payload: dict | None = None
    response_record_id: UUID | None = None
    applied: bool | None = None
    ecs_attempt_outcome: str | None = None
    attempt_revision: int | None = None


@dataclass(frozen=True)
class StoredBody:
    record_id: UUID
    body_state: str
    raw_body: bytes | None
    body_sha256: str | None
    stored_bytes: int | None


@dataclass(frozen=True)
class SourceRef:
    kind: str
    identity: str
    revision: str | int | None = None


def encode_reply(body: dict | None) -> bytes:
    return (
        b""
        if body is None
        else json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def reply_protocol_code(prepared_body: bytes) -> str | None:
    try:
        body = json.loads(prepared_body)
    except (ValueError, UnicodeError, RecursionError):
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return (
        str(code)
        if type(code) in {str, int} and len(str(code)) <= 128 and "\x00" not in str(code)
        else None
    )


async def write_record(session, *, draft: RecordDraft) -> UUID:
    values = asdict(draft)
    if draft.details is not None:
        allowed = {
            "trace_id",
            "origin",
            "failure_kind",
            "fail_reason",
            "pause_reason",
            "projection_state",
            "response_scope",
            "source",
            "request_id",
            "truncated",
            "encoding",
            "coverage_reason",
            "owner_domain",
            "action_id",
            "source_kind",
            "source_identity",
            "source_revision",
            "trigger_record_id",
            "diagnostic",
            "outcome",
            "attempt_count",
            "recorded_delivery_state",
        }
        try:
            encoded = json.dumps(draft.details, allow_nan=False, ensure_ascii=False)
            pending = [(draft.details, 0)]
            valid = isinstance(draft.details, dict) and set(draft.details) <= allowed
            while pending and valid:
                item, depth = pending.pop()
                if depth > 4:
                    valid = False
                elif isinstance(item, dict):
                    pending.extend((value, depth + 1) for value in item.values())
                    pending.extend((key, depth + 1) for key in item)
                elif isinstance(item, list):
                    pending.extend((value, depth + 1) for value in item)
                elif isinstance(item, str) and "\x00" in item:
                    valid = False
            if not valid or len(encoded.encode("utf-8")) > 4096:
                values["details"] = {"projection_state": "UNAVAILABLE"}
        except (TypeError, ValueError, RecursionError):
            values["details"] = {"projection_state": "UNAVAILABLE"}
    body = values["raw_body"]
    if body is not None and (not isinstance(body, bytes) or draft.body_record_id is not None):
        raise EvidenceIntegrityError("invalid canonical body")
    values["body_sha256"] = sha256(body).hexdigest() if body is not None else None
    values["stored_bytes"] = len(body) if body is not None else None
    values["body_state"] = draft.body_state or (
        "FULL" if body is not None or draft.body_record_id else "UNAVAILABLE"
    )
    if not draft.event_key or len(draft.event_key) > 256:
        raise EvidenceIntegrityError("invalid fact identity")
    if draft.body_record_id is not None:
        await _lock_body_reference(
            session,
            draft.body_record_id,
            core=draft.evidence_class == "CORE",
            direct=True,
        )
    if draft.evidence_class == "CORE" and draft.response_record_id is not None:
        await _lock_body_reference(session, draft.response_record_id, core=True, direct=False)
    query = (
        select(IntegrationRecord)
        .where(
            IntegrationRecord.exchange_id == draft.exchange_id,
            IntegrationRecord.event_key == draft.event_key,
        )
        .execution_options(populate_existing=True)
    )

    def equal(existing):
        # Provenance and local clocks belong to the first saved fact.
        if any(
            getattr(existing, key) != value
            for key, value in values.items()
            if key != "capture_origin"
        ):
            raise EvidenceIntegrityError("fact identity has different content")
        return existing.id

    try:
        with session.no_autoflush:
            existing = await session.scalar(query)
        if existing is not None:
            return equal(existing)
        await ensure_savepoint_transaction(session)
        try:
            async with session.begin_nested():
                record = IntegrationRecord(id=uuid7(), **values)
                session.add(record)
                await session.flush()
        except IntegrityError:
            with session.no_autoflush:
                existing = await session.scalar(query)
            if existing is None:
                raise
            return equal(existing)
        return record.id
    except SQLAlchemyError as error:
        raise EvidencePersistenceError("integration evidence unavailable") from error


async def _lock_body_reference(session, identity, *, core, direct):
    original = identity
    for hop in range(2):
        with session.no_autoflush:
            target = (
                await session.execute(
                    select(IntegrationRecord.body_record_id, IntegrationRecord.evidence_class)
                    .where(IntegrationRecord.id == identity)
                    .with_for_update(read=True, key_share=True)
                )
            ).first()
        if target is None:
            raise EvidenceIntegrityError("canonical body unavailable")
        if core and target.evidence_class != "CORE":
            raise EvidenceIntegrityError("diagnostics cannot supply authoritative bytes")
        if target.body_record_id is None:
            break
        if direct or hop:
            raise EvidenceIntegrityError("canonical body cannot be a reference")
        identity = target.body_record_id
    # Keep borrowed bytes protected from cleanup until the caller commits the new authority.
    body = (await read_bodies(session, record_ids=(original,))).get(original)
    if body is None or body.raw_body is None:
        raise EvidenceIntegrityError("canonical body unavailable")


async def read_bodies(session, *, record_ids: tuple[UUID, ...]) -> dict[UUID, StoredBody]:
    if not record_ids:
        return {}
    record, body = aliased(IntegrationRecord), aliased(IntegrationRecord)
    with session.no_autoflush:
        rows = await session.execute(
            select(
                record.id,
                record.body_state,
                record.raw_body,
                record.body_sha256,
                record.stored_bytes,
                record.body_record_id,
                body.id.label("canonical_id"),
                body.body_state.label("canonical_state"),
                body.raw_body.label("canonical_raw"),
                body.body_sha256.label("canonical_sha"),
                body.stored_bytes.label("canonical_size"),
                body.body_record_id.label("chain"),
            )
            .outerjoin(body, body.id == record.body_record_id)
            .where(record.id.in_(record_ids))
        )
    result = {}
    for row in rows.mappings():
        if row["body_record_id"] is not None:
            if row["canonical_id"] is None or row["chain"] is not None:
                raise EvidenceIntegrityError("invalid canonical body reference")
            stored = StoredBody(
                row["id"],
                row["canonical_state"],
                row["canonical_raw"],
                row["canonical_sha"],
                row["canonical_size"],
            )
        else:
            stored = StoredBody(
                row["id"],
                row["body_state"],
                row["raw_body"],
                row["body_sha256"],
                row["stored_bytes"],
            )
        if stored.raw_body is not None and (
            stored.stored_bytes != len(stored.raw_body)
            or stored.body_sha256 != sha256(stored.raw_body).hexdigest()
        ):
            raise EvidenceIntegrityError("stored body integrity mismatch")
        result[row["id"]] = stored
    return result


def body_projection(from_clause, reference, *, name):
    """Attach one canonical body to an existing read statement without another query."""
    record = IntegrationRecord.__table__.alias(name + "_record")
    body = IntegrationRecord.__table__.alias(name + "_body")
    from_clause = from_clause.outerjoin(record, record.c.id == reference).outerjoin(
        body, body.c.id == record.c.body_record_id
    )
    columns = {}
    for key, column in (
        ("raw", "raw_body"),
        ("state", "body_state"),
        ("sha", "body_sha256"),
        ("size", "stored_bytes"),
    ):
        columns[name + "_" + key] = case(
            (record.c.body_record_id.is_not(None), body.c[column]), else_=record.c[column]
        ).label(name + "_" + key)
    columns[name + "_chain"] = body.c.body_record_id.label(name + "_chain")
    return from_clause, columns


def projected_body(row, *, name):
    raw = row[name + "_raw"]
    if row[name + "_chain"] is not None:
        raise EvidenceIntegrityError("invalid canonical body reference")
    if raw is not None and (
        row[name + "_size"] != len(raw) or row[name + "_sha"] != sha256(raw).hexdigest()
    ):
        raise EvidenceIntegrityError("stored body integrity mismatch")
    return raw if row[name + "_state"] in {"FULL", "LEGACY_REENCODED"} else None


async def matching_body_reference(session, *, record_id: UUID | None, raw_body: bytes):
    if record_id is None:
        return None
    bodies = await read_bodies(session, record_ids=(record_id,))
    existing = bodies.get(record_id)
    if existing is None or existing.raw_body != raw_body:
        return None
    with session.no_autoflush:
        canonical = await session.scalar(
            select(IntegrationRecord.body_record_id).where(IntegrationRecord.id == record_id)
        )
    return canonical or record_id


async def register(session, *, message, protocol: str, exchange_id: UUID | None = None):
    exchange_id = exchange_id or message.exchange_id
    if exchange_id is None:
        exchange = IntegrationExchange(
            id=uuid7(),
            role="OUTGOING",
            peer_system="WMS" if protocol.startswith("wms-") else None,
            protocol=protocol,
            direction="OUTBOUND",
            interaction_kind="REQUEST",
            operation=message.operation,
            operation_id=message.operation_id,
            identity_kind="OPERATION_ID",
            identity_value=message.operation_id,
            target_key=message.target,
            owner_domain=message.owner_domain,
            created_at=message.created_at,
        )
        session.add(exchange)
        await session.flush()
        exchange_id = exchange.id
    else:
        exchange = await session.get(IntegrationExchange, exchange_id)
        if exchange is None:
            raise EvidenceIntegrityError("original exchange unavailable")
    if message.request_record_id is not None:
        request_id = message.request_record_id
        bodies = await requests(session, record_ids=(request_id,))
        if bodies[request_id] != message.request_raw or (
            exchange.request_sha256 is not None
            and exchange.request_sha256 != sha256(message.request_raw).hexdigest()
        ):
            raise EvidenceIntegrityError("frozen request mismatch")
    else:
        request_id = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=exchange_id,
                record_kind="REQUEST_REGISTERED",
                event_key="REQUEST",
                direction="OUTBOUND",
                occurred_at=message.created_at,
                raw_body=message.request_raw,
                details={"trace_id": message.trace_id},
            ),
        )
    exchange.request_record_id = request_id
    return exchange_id, request_id


async def requests(session, *, record_ids: tuple[UUID, ...]) -> dict[UUID, bytes]:
    bodies = await read_bodies(session, record_ids=record_ids)
    if any(
        identity not in bodies
        or bodies[identity].body_state != "FULL"
        or bodies[identity].raw_body is None
        for identity in record_ids
    ):
        raise EvidenceIntegrityError("frozen request unavailable")
    return {identity: bodies[identity].raw_body for identity in record_ids}


async def opened(session, *, claim, at: datetime) -> UUID:
    if not claim.lease_id or claim.message.exchange_id is None:
        raise EvidenceIntegrityError("send gate requires original identity")
    key = f"OPEN:{claim.lease_id}"
    original = await session.scalar(
        select(IntegrationRecord).where(
            IntegrationRecord.exchange_id == claim.message.exchange_id,
            IntegrationRecord.event_key == key,
        )
    )
    return await write_record(
        session,
        draft=RecordDraft(
            exchange_id=claim.message.exchange_id,
            record_kind="SEND_ATTEMPT_OPENED",
            event_key=key,
            direction="OUTBOUND",
            attempt_id=claim.lease_id,
            parent_record_id=claim.message.request_record_id,
            occurred_at=original.occurred_at if original else at,
            details={"origin": claim.dispatch_origin},
        ),
    )


async def observe_response(session, *, claim, outcome) -> UUID | None:
    if outcome.result is None or (
        outcome.result.body is None and outcome.result.status_code is None
    ):
        return None
    return await write_record(
        session,
        draft=RecordDraft(
            exchange_id=claim.message.exchange_id,
            record_kind="HTTP_RESPONSE_OBSERVED",
            event_key=f"HTTP:{claim.lease_id}",
            attempt_id=claim.lease_id,
            direction="INBOUND",
            raw_body=outcome.result.body,
            body_state="PREVIEW"
            if (
                outcome.result.body is not None
                and outcome.result.delivery_state != "HTTP_RESPONSE_COMPLETE"
            )
            else None,
            http_status=outcome.result.status_code,
            network_state=outcome.result.delivery_state,
            protocol_verdict=outcome.verdict.kind if outcome.verdict else None,
            details={"failure_kind": outcome.result.failure_kind},
        ),
    )


async def finished(session, *, claim, outcome, at: datetime, late: bool) -> UUID | None:
    response_id = await observe_response(session, claim=claim, outcome=outcome)
    key = f"FINISH:{claim.lease_id}:{outcome.status}:{outcome.pause_reason}:{int(late)}"
    original = await session.scalar(
        select(IntegrationRecord).where(
            IntegrationRecord.exchange_id == claim.message.exchange_id,
            IntegrationRecord.event_key == key,
        )
    )
    finish_id = await write_record(
        session,
        draft=RecordDraft(
            exchange_id=claim.message.exchange_id,
            record_kind="SEND_ATTEMPT_FINISHED",
            event_key=key,
            attempt_id=claim.lease_id,
            direction="INTERNAL",
            late=late,
            occurred_at=original.occurred_at if original else at,
            parent_record_id=response_id,
            body_record_id=response_id
            if outcome.result is not None and outcome.result.body is not None
            else None,
            application_state=outcome.status,
            network_state=outcome.result.delivery_state if outcome.result else None,
            details={"fail_reason": outcome.fail_reason, "pause_reason": outcome.pause_reason},
        ),
    )
    return finish_id


async def write_reference(
    session,
    *,
    exchange_id,
    reference_kind,
    namespace,
    value,
    relation_role,
    created_at,
    source_record_id=None,
    related_exchange_id=None,
):
    digest = sha256(value.encode()).hexdigest()
    query = select(IntegrationReference).where(
        IntegrationReference.exchange_id == exchange_id,
        IntegrationReference.reference_kind == reference_kind,
        IntegrationReference.namespace == namespace,
        IntegrationReference.value_digest == digest,
        IntegrationReference.relation_role == relation_role,
        IntegrationReference.source_record_id == source_record_id,
    )
    existing = await session.scalar(query)
    if existing is not None:
        if existing.reference_value != value or existing.related_exchange_id != related_exchange_id:
            raise EvidenceIntegrityError("original reference mismatch")
        return existing.id
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    identity = await session.scalar(
        insert(IntegrationReference)
        .values(
            exchange_id=exchange_id,
            reference_kind=reference_kind,
            namespace=namespace,
            reference_value=value,
            value_digest=digest,
            relation_role=relation_role,
            source_record_id=source_record_id,
            related_exchange_id=related_exchange_id,
            created_at=created_at,
        )
        .on_conflict_do_nothing()
        .returning(IntegrationReference.id)
    )
    if identity is not None:
        return identity
    existing = await session.scalar(query)
    if (
        existing is None
        or existing.reference_value != value
        or existing.related_exchange_id != related_exchange_id
    ):
        raise EvidenceIntegrityError("original reference mismatch")
    return existing.id


async def link_message(session, *, exchange_id, message_id, at):
    return await write_reference(
        session,
        exchange_id=exchange_id,
        reference_kind="RELIABLE_MESSAGE",
        namespace="id",
        value=str(message_id),
        relation_role="ORIGINAL_MESSAGE",
        created_at=at,
    )


async def _source_fact(session, source, owner_domain):
    from app.models import ReliableMessage, TransportTask

    parent_id, exchange_id = None, None
    with session.no_autoflush:
        if source.kind == "ECS_COMMAND":
            root = await session.scalar(
                select(IntegrationExchange).where(
                    IntegrationExchange.role == "ECS_COMMAND",
                    IntegrationExchange.command_code == source.identity,
                    *(
                        [IntegrationExchange.owner_domain == owner_domain]
                        if owner_domain is not None
                        else []
                    ),
                )
            )
            if root:
                exchange_id = root.id
                parent_id = await session.scalar(
                    select(IntegrationRecord.id).where(
                        IntegrationRecord.exchange_id == root.id,
                        IntegrationRecord.record_kind == "RESULT_EVIDENCE",
                        IntegrationRecord.semantic_digest == str(source.revision),
                    )
                )
        elif source.kind == "WMS_MESSAGE":
            row = (
                await session.execute(
                    select(
                        ReliableMessage.exchange_id, ReliableMessage.terminal_response_record_id
                    ).where(ReliableMessage.id == int(source.identity))
                )
            ).one_or_none()
            if row:
                exchange_id, parent_id = row
            else:
                matches = list(
                    await session.scalars(
                        select(IntegrationReference.exchange_id).where(
                            IntegrationReference.reference_kind == "RELIABLE_MESSAGE",
                            IntegrationReference.namespace == "id",
                            IntegrationReference.reference_value == source.identity,
                            IntegrationReference.value_digest
                            == sha256(source.identity.encode()).hexdigest(),
                            IntegrationReference.relation_role == "ORIGINAL_MESSAGE",
                        )
                    )
                )
                exchange_id = matches[0] if len(matches) == 1 else None
        elif source.kind == "TRANSPORT_TASK":
            task = (
                await session.execute(
                    select(
                        TransportTask.submission_exchange_id,
                        TransportTask.acceptance_response_record_id,
                    ).where(TransportTask.transport_task_id == source.identity)
                )
            ).one_or_none()
            if task:
                exchange_id = task.submission_exchange_id
                if source.revision is None:
                    parent_id = task.acceptance_response_record_id
                else:
                    parent_id = await session.scalar(
                        select(IntegrationRecord.id).where(
                            IntegrationRecord.exchange_id == exchange_id,
                            IntegrationRecord.record_kind == "RESULT_APPLIED",
                            IntegrationRecord.outcome_revision == source.revision,
                        )
                    )
        else:
            raise ValueError("unsupported consumption source")
    return exchange_id, parent_id


async def link_source(
    session, *, source, owner_domain, reference_kind, namespace, value, relation_role, at
):
    exchange_id, _ = await _source_fact(session, source, owner_domain)
    if exchange_id is None:
        return None
    return await write_reference(
        session,
        exchange_id=exchange_id,
        reference_kind=reference_kind,
        namespace=namespace,
        value=value,
        relation_role=relation_role,
        created_at=at,
    )


async def record_consumption(
    session, *, owner_domain: str, action_id: str, source: SourceRef, consumed_at: datetime
) -> UUID | None:
    exchange_id, parent_id = await _source_fact(session, source, owner_domain)
    if exchange_id is None:
        return None
    source_version = (
        str(source.revision) if source.revision is not None else str(parent_id or "UNAVAILABLE")
    )
    identity = json.dumps(
        [owner_domain, action_id, source.kind, source.identity, source_version],
        separators=(",", ":"),
    )
    event_key = "CONSUMED:" + sha256(identity.encode()).hexdigest()
    existing = await session.scalar(
        select(IntegrationRecord).where(
            IntegrationRecord.exchange_id == exchange_id, IntegrationRecord.event_key == event_key
        )
    )
    record_id = await write_record(
        session,
        draft=RecordDraft(
            exchange_id=exchange_id,
            record_kind="BUSINESS_CONSUMED",
            event_key=event_key,
            direction="INTERNAL",
            occurred_at=existing.occurred_at if existing else consumed_at,
            parent_record_id=parent_id,
            source_kind="BUSINESS_ACTION",
            source_key=json.dumps([owner_domain, action_id]),
            source_version=sha256(identity.encode()).hexdigest(),
            details={
                "owner_domain": owner_domain,
                "action_id": action_id,
                "source_kind": source.kind,
                "source_identity": source.identity,
                "source_revision": source_version,
                "coverage_reason": "ORIGINAL_REFERENCE"
                if parent_id
                else "SOURCE_EVIDENCE_UNAVAILABLE",
            },
        ),
    )
    await write_reference(
        session,
        exchange_id=exchange_id,
        reference_kind="BUSINESS_ACTION",
        namespace=owner_domain,
        value=action_id,
        relation_role="CONSUMED_BY",
        source_record_id=record_id,
        created_at=existing.occurred_at if existing else consumed_at,
    )
    return record_id


async def link_receipt(
    session,
    *,
    receipt_id,
    reference_kind,
    namespace,
    value,
    relation_role,
    at,
    related_exchange_id=None,
):
    exchange_id = await session.scalar(
        select(IntegrationExchange.id).where(
            IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_id == receipt_id
        )
    )
    if exchange_id is None:
        return None
    return await write_reference(
        session,
        exchange_id=exchange_id,
        reference_kind=reference_kind,
        namespace=namespace,
        value=value,
        relation_role=relation_role,
        created_at=at,
        related_exchange_id=related_exchange_id,
    )
