"""Offline evidence copy. This module never assembles delivery or business progression."""

import argparse
import asyncio
import base64
import binascii
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from alembic.util import CommandError
from sqlalchemy import JSON, MetaData, Table, Uuid, and_, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine
from app.integrations.history import (
    RecordDraft,
    encode_reply,
    read_bodies,
    reply_protocol_code,
    write_record,
    write_reference,
)
from app.models import IntegrationExchange as Exchange
from app.models import IntegrationRecord as Record
from app.models import IntegrationReference as Reference
from migrations.integration_history_v1 import (
    SOURCES,
    snapshot_version,
    transport_submission_candidates,
    validate_originals,
    validate_transport_exchange,
)

WRITERS = (
    "api",
    "sender",
    "worker",
    "beat",
    "background",
    "one_off",
    "manual",
    "database_transactions",
)
LOCK_KEY = 682701106


@dataclass(frozen=True)
class BatchResult:
    source: str
    next_cursor: int | str | None
    copied: int
    reused: int
    conflicts: int
    done: bool


@dataclass(frozen=True)
class WriterEvidence:
    inventory: dict[str, str]
    attestation: str

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {*WRITERS, "attestation"}:
            raise ValueError("writer inventory must explicitly include every category")
        if any(value[key] not in {"DRAINED", "UNKNOWN", "RUNNING"} for key in WRITERS):
            raise ValueError("invalid writer status")
        proof = value["attestation"]
        if (
            not isinstance(proof, str)
            or not 1 <= len(proof) <= 128
            or any(not (c.isalnum() or c in "-_.") for c in proof)
        ):
            raise ValueError("attestation must be a bounded evidence identifier")
        return cls({key: value[key] for key in WRITERS}, proof)

    @property
    def status(self):
        values = self.inventory.values()
        return "RUNNING" if "RUNNING" in values else "UNKNOWN" if "UNKNOWN" in values else "DRAINED"


@dataclass(frozen=True)
class ReconciliationReport:
    source_counts: dict[str, int]
    identity_mismatches: int
    byte_mismatches: int
    missing_refs: int
    writer_status: str
    sequence_highwater: int
    eligible: bool


class Conflict(Exception):
    """Safe classification only; never includes an original payload or SQL."""


async def source_table(session, source):
    if source not in SOURCES:
        raise ValueError("unknown source")
    cached = session.info.setdefault("legacy_history_tables", {})
    if source in cached:
        return cached[source]
    connection = await session.connection()
    schema = "enterprise_backend" if connection.dialect.name == "postgresql" else None
    table = await connection.run_sync(
        lambda c: Table(source, MetaData(), schema=schema, autoload_with=c, resolve_fks=False)
    )
    for name in (
        "exchange_id",
        "request_record_id",
        "terminal_response_record_id",
        "last_response_record_id",
        "submission_exchange_id",
        "acceptance_request_record_id",
        "acceptance_response_record_id",
    ):
        if name in table.c:
            table.c[name].type = Uuid()
    cached[source] = table
    return table


async def _mapped(session, source, key):
    return await session.scalar(
        select(Exchange)
        .join(Reference, Reference.exchange_id == Exchange.id)
        .where(
            Reference.reference_kind == "LEGACY_SOURCE",
            Reference.namespace == source,
            Reference.value_digest == sha256(str(key).encode()).hexdigest(),
            Reference.reference_value == str(key),
        )
    )


async def _associate(session, root, source, key, at):
    await write_reference(
        session,
        exchange_id=root.id,
        reference_kind="LEGACY_SOURCE",
        namespace=source,
        value=str(key),
        relation_role="ORIGINAL_SOURCE",
        created_at=at,
    )


async def _root(session, source, row, **values):
    key = row["command_code"] if source == "ecs_command" else row["id"]
    root = await _mapped(session, source, key)
    if root is None and source in {"ecs_command", "callback_receipt"}:
        predicate = (
            Exchange.command_code == key if source == "ecs_command" else Exchange.receipt_id == key
        )
        root = await session.scalar(select(Exchange).where(predicate))
    if root is None:
        root = Exchange(capture_origin="BACKFILL", coverage="LEGACY", **values)
        session.add(root)
        await session.flush()
    elif root.capture_origin != "BACKFILL":
        raise Conflict("identity")
    await _associate(session, root, source, key, values["created_at"])
    return root


async def _record(
    session,
    root,
    source,
    key,
    version,
    slot,
    *,
    kind="LEGACY_SNAPSHOT",
    raw=None,
    at=None,
    **values,
):
    return await write_record(
        session,
        draft=RecordDraft(
            exchange_id=root.id,
            record_kind=kind,
            event_key=f"BACKFILL:{source}:{key}:{slot}:{version}",
            direction=values.pop("direction", "INTERNAL"),
            capture_origin="BACKFILL",
            occurred_at=at,
            source_kind=source + ":" + slot,
            source_key=str(key),
            source_version=version,
            raw_body=raw,
            **values,
        ),
    )


async def _body_matches(session, identity, raw):
    if identity is None:
        return raw is None
    body = (await read_bodies(session, record_ids=(identity,))).get(identity)
    return body is not None and body.raw_body == raw


async def _command(session, row, version):
    fields = {
        key: row[key]
        for key in SOURCES["ecs_command"].split()
        if key not in {"request_raw", "ack_raw"}
    }
    root = await _root(
        session,
        "ecs_command",
        row,
        role="ECS_COMMAND",
        protocol="ecs-command",
        peer_system="ECS",
        direction="OUTBOUND",
        interaction_kind="COMMAND",
        identity_kind="COMMAND_CODE",
        identity_value=row["command_code"],
        **fields,
    )
    for field in (
        "owner_domain",
        "call_key",
        "call_key_digest",
        "input_raw",
        "device_code",
        "task_type",
        "route",
        "target",
        "delivery_snapshot",
        "expires_at",
        "created_at",
    ):
        old, new = getattr(root, field), row[field]
        if snapshot_version({field: old}) != snapshot_version({field: new}):
            raise Conflict("identity")
    if root.request_record_id and not await _body_matches(
        session, root.request_record_id, row["request_raw"]
    ):
        raise Conflict("bytes")
    root.request_record_id = await _record(
        session,
        root,
        "ecs_command",
        row["command_code"],
        version,
        "request",
        kind="REQUEST_REGISTERED",
        raw=row["request_raw"],
        at=row["created_at"],
        direction="OUTBOUND",
    )
    root.request_sha256 = sha256(row["request_raw"]).hexdigest()
    if row["ack_raw"] is not None:
        root.ack_record_id = await _record(
            session,
            root,
            "ecs_command",
            row["command_code"],
            version,
            "ack",
            kind="HTTP_RESPONSE_OBSERVED",
            raw=row["ack_raw"],
            direction="INBOUND",
        )
    for field in (
        "status",
        "delivery_status",
        "dispatch_started_at",
        "hold_reason",
        "blocked",
        "needs_reconciliation",
        "applied_result_id",
    ):
        setattr(root, field, row[field])
    return root


async def _receipt(session, row, version):
    operation = row["operation"]
    ecs = operation in {"ecs.event", "ecs.result"}
    fields = {key: value for key, value in row.items() if key not in {"id", "body"}}
    root = await _root(
        session,
        "callback_receipt",
        row,
        role="RECEIPT",
        protocol="ecs-result"
        if operation == "ecs.result"
        else "ecs-event"
        if ecs
        else "wms-callback",
        peer_system="ECS" if ecs else "WMS",
        direction="INBOUND",
        interaction_kind="EVENT" if operation == "ecs.event" else "CALLBACK",
        identity_kind="EVENT_DIGEST" if ecs else "OPERATION_ID",
        identity_value=row["operation_id"],
        receipt_id=row["id"],
        receipt_active=True,
        **fields,
    )
    for field in (
        "operation",
        "operation_id",
        "raw_digest",
        "compare_mode",
        "effective_digest",
        "created_at",
    ):
        if snapshot_version({field: getattr(root, field)}) != snapshot_version({field: row[field]}):
            raise Conflict("identity")
    for field in (
        "device_code",
        "status_code",
        "resp_timestamp",
        "event_envelope",
        "event_context",
        "application_ref",
        "diagnostic",
    ):
        setattr(root, field, row[field])
    root.current_response_record_id = await _record(
        session,
        root,
        "callback_receipt",
        row["id"],
        version,
        "reply",
        kind="RESPONSE_PREPARED",
        raw=encode_reply(row["body"]),
        body_state="LEGACY_REENCODED",
        http_status=row["status_code"],
        protocol_code=reply_protocol_code(encode_reply(row["body"])),
        at=row["created_at"],
        direction="OUTBOUND",
    )
    if operation == "ecs.event":
        owner = (
            await session.execute(
                text(
                    "SELECT id,work_line_id,workline_code FROM coarse_sorting_job "
                    "WHERE receipt_id=:receipt AND event_digest=:digest"
                ),
                {"receipt": row["id"], "digest": row["operation_id"]},
            )
        ).one_or_none()
        if owner is not None:
            context = row["event_context"] or {}
            if context.get(
                "work_line_id", owner.work_line_id
            ) != owner.work_line_id or root.owner_domain not in {None, "coarse_sorting"}:
                raise Conflict("identity")
            root.owner_domain = "coarse_sorting"
            for kind, namespace, value in (
                ("BUSINESS_TASK", "coarse_sorting", owner.id),
                ("WORKLINE", "id", str(owner.work_line_id)),
                ("WORKLINE", "code", owner.workline_code),
            ):
                await write_reference(
                    session,
                    exchange_id=root.id,
                    reference_kind=kind,
                    namespace=namespace,
                    value=value,
                    relation_role="ORIGINAL_JOB",
                    created_at=row["created_at"],
                )
    return root


async def _guarded_bind(session, table, row, values, *, bindings):
    locked = (
        (
            await session.execute(
                select(*(table.c[name] for name in (*row, *bindings)))
                .where(
                    table.c.command_code == row["command_code"]
                    if table.name == "ecs_command"
                    else table.c.id == row["id"]
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        locked is None
        or snapshot_version({key: locked[key] for key in row}) != snapshot_version(row)
        or any(locked[key] != value for key, value in bindings.items())
    ):
        raise Conflict("identity")
    predicates = []
    for key, value in row.items():
        column = table.c[key]
        if isinstance(column.type, JSON):
            # JSON text order is not an identity; version is rechecked under the row lock below.
            continue
        predicates.append(column.is_(None) if value is None else column == value)
    result = await session.execute(update(table).where(and_(*predicates)).values(**values))
    if result.rowcount != 1:
        raise Conflict("identity")


async def _message(session, row, version):
    table = await source_table(session, "reliable_message")
    existing = (
        await session.execute(
            select(
                table.c.exchange_id,
                table.c.request_record_id,
                table.c.terminal_response_record_id,
                table.c.last_response_record_id,
            ).where(table.c.id == row["id"])
        )
    ).one()
    command = (
        await session.scalar(
            select(Exchange).where(
                Exchange.role == "ECS_COMMAND",
                Exchange.command_code == row["operation_id"],
                Exchange.target == row["target"],
            )
        )
        if row["operation"] == "ecs.command"
        else None
    )
    if row["operation"] == "ecs.command" and command is None:
        raise Conflict("missing")
    root = (
        await session.get(Exchange, existing.exchange_id)
        if existing.exchange_id
        else command or await _mapped(session, "reliable_message", row["id"])
    )
    if root is None:
        protocol = (row["delivery_snapshot"] or {}).get("protocol", "legacy-outgoing")
        root = await _root(
            session,
            "reliable_message",
            row,
            role="OUTGOING",
            protocol=protocol,
            peer_system="WMS" if protocol.startswith("wms-") else None,
            direction="OUTBOUND",
            interaction_kind="REQUEST",
            identity_kind="OPERATION_ID",
            identity_value=row["operation_id"],
            operation=row["operation"],
            operation_id=row["operation_id"],
            target_key=row["target"],
            created_at=row["created_at"],
        )
    if root.capture_origin != "BACKFILL" or (command and root.id != command.id):
        raise Conflict("identity")
    if root.role == "ECS_COMMAND" and not await _body_matches(
        session, root.request_record_id, row["request_raw"]
    ):
        raise Conflict("bytes")
    if existing.request_record_id and not await _body_matches(
        session, existing.request_record_id, row["request_raw"]
    ):
        raise Conflict("bytes")
    await _associate(session, root, "reliable_message", row["id"], row["created_at"])
    refs = {}
    for column, source, slot in (
        ("request_record_id", "request_raw", "request"),
        ("terminal_response_record_id", "response_raw", "terminal"),
        ("last_response_record_id", "last_response_raw", "last"),
    ):
        raw = row[source]
        refs[column] = (
            None
            if raw is None
            else await _record(
                session,
                root,
                "reliable_message",
                row["id"],
                version,
                slot,
                kind="REQUEST_REGISTERED" if slot == "request" else "HTTP_RESPONSE_OBSERVED",
                raw=None if command and slot == "request" else raw,
                body_record_id=command.request_record_id if command and slot == "request" else None,
                at=row["created_at"] if slot == "request" else None,
                direction="OUTBOUND" if slot == "request" else "INBOUND",
                http_status=None if slot == "request" else row["last_http_status"],
            )
        )
    if command is not None:
        refs["request_record_id"] = command.request_record_id
    if refs["request_record_id"] is None:
        raise Conflict("bytes")
    if root.request_record_id is None:
        root.request_record_id = refs["request_record_id"]
    await _guarded_bind(
        session, table, row, {"exchange_id": root.id, **refs}, bindings=existing._mapping
    )
    await write_reference(
        session,
        exchange_id=root.id,
        reference_kind="RELIABLE_MESSAGE",
        namespace="id",
        value=str(row["id"]),
        relation_role="ORIGINAL_MESSAGE",
        created_at=row["created_at"],
    )
    return root


async def _send(session, row, version):
    root = await session.scalar(
        select(Exchange).where(Exchange.command_code == row["command_code"])
    )
    if root is None:
        raise Conflict("missing")
    prior = (
        await session.scalar(
            select(func.max(Record.attempt_revision)).where(
                Record.exchange_id == root.id, Record.attempt_id == row["lease_id"]
            )
        )
        or 0
    )
    existing = await session.scalar(
        select(Record).where(
            Record.source_kind == "ecs_send_evidence:conclusion",
            Record.source_key == str(row["id"]),
            Record.source_version == version,
        )
    )
    if existing is None:
        if row["outcome"] == "OPEN_ATTEMPT":
            await _record(
                session,
                root,
                "ecs_send_evidence",
                row["id"],
                version,
                "conclusion",
                kind="SEND_ATTEMPT_OPENED",
                attempt_id=row["lease_id"],
                at=row["created_at"],
            )
        else:
            await _record(
                session,
                root,
                "ecs_send_evidence",
                row["id"],
                version,
                "conclusion",
                kind="ECS_ATTEMPT_CONCLUSION",
                attempt_id=row["lease_id"],
                attempt_revision=prior + 1,
                ecs_attempt_outcome=row["outcome"],
                raw=row["response_raw"],
                http_status=row["http_status"],
                late=row["late"],
            )
    return root


async def _result(session, row, version):
    root = await session.scalar(
        select(Exchange).where(Exchange.command_code == row["command_code"])
    )
    if root is None:
        raise Conflict("missing")
    existing = await session.scalar(
        select(Record).where(Record.exchange_id == root.id, Record.semantic_digest == row["digest"])
    )
    if existing:
        if (
            existing.effective_payload != row["core"]
            or existing.applied != row["applied"]
            or existing.http_status != row["status_code"]
            or not await _body_matches(
                session, existing.response_record_id, encode_reply(row["response"])
            )
        ):
            raise Conflict("bytes")
        return root
    reply = await _record(
        session,
        root,
        "ecs_result_evidence",
        row["id"],
        version,
        "reply",
        kind="RESPONSE_PREPARED",
        raw=encode_reply(row["response"]),
        body_state="LEGACY_REENCODED",
        http_status=row["status_code"],
        at=row["created_at"],
        direction="OUTBOUND",
    )
    await _record(
        session,
        root,
        "ecs_result_evidence",
        row["id"],
        version,
        "result",
        kind="RESULT_EVIDENCE",
        raw=encode_reply(row["core"]),
        body_state="LEGACY_REENCODED",
        semantic_digest=row["digest"],
        effective_payload=row["core"],
        response_record_id=reply,
        applied=row["applied"],
        http_status=row["status_code"],
        at=row["created_at"],
        direction="INBOUND",
    )
    return root


async def _snapshot(session, row, version):
    source = row["source"] or "wms"
    root = await _root(
        session,
        "callback_snapshot",
        row,
        role="DIAGNOSTIC",
        protocol="wms-callback"
        if source == "wms"
        else "ecs-event"
        if source == "ecs"
        else "ecs-result",
        peer_system="WMS" if source == "wms" else "ECS",
        direction="INBOUND",
        interaction_kind="CALLBACK",
        identity_kind="LEGACY_SNAPSHOT",
        identity_value=str(row["id"]),
        created_at=row["received_at"],
    )
    invalid = False
    try:
        raw = base64.b64decode(row["body_b64"], validate=True)
    except (ValueError, binascii.Error):
        raw, invalid = None, True
    await _record(
        session,
        root,
        "callback_snapshot",
        row["id"],
        version,
        "request",
        kind="CALLBACK_RECEIVED",
        raw=raw,
        body_state="UNAVAILABLE" if invalid else "PREVIEW",
        evidence_class="DIAGNOSTIC",
        at=row["received_at"],
        direction="INBOUND",
        details={
            "source": source,
            "truncated": row["truncated"],
            "coverage_reason": "INVALID_BASE64" if invalid else "LEGACY_PREVIEW",
        },
    )
    if row["response_body"] is not None:
        await _record(
            session,
            root,
            "callback_snapshot",
            row["id"],
            version,
            "reply",
            kind="RESPONSE_PREPARED",
            raw=encode_reply(row["response_body"]),
            body_state="LEGACY_REENCODED",
            evidence_class="DIAGNOSTIC",
            http_status=row["http_status"],
            at=row["received_at"],
            direction="OUTBOUND",
        )
    return root, int(invalid)


async def _transport(session, row, version):
    root = await _mapped(session, "transport_task", row["id"])
    table = await source_table(session, "transport_task")
    bindings = (
        await session.execute(
            select(
                table.c.submission_exchange_id,
                table.c.acceptance_request_record_id,
                table.c.acceptance_response_record_id,
            ).where(table.c.id == row["id"])
        )
    ).one()
    if bindings.submission_exchange_id:
        if root is not None and root.id != bindings.submission_exchange_id:
            raise Conflict("identity")
        root = await session.get(Exchange, bindings.submission_exchange_id)
        if root is None:
            raise Conflict("identity")
    messages = await source_table(session, "reliable_message")
    try:
        candidates = (
            (await session.execute(transport_submission_candidates(messages, row))).mappings().all()
        )
    except CommandError:
        raise Conflict("identity") from None
    if len(candidates) > 1 or (candidates and candidates[0]["exchange_id"] is None):
        raise Conflict("identity")
    if root is None:
        root = (
            await session.get(Exchange, candidates[0]["exchange_id"])
            if candidates
            else await _root(
                session,
                "transport_task",
                row,
                role="OUTGOING",
                protocol="wms-transport",
                peer_system="WMS",
                direction="OUTBOUND",
                interaction_kind="REQUEST",
                identity_kind="TRANSPORT_TASK_ID",
                identity_value=row["transport_task_id"],
                operation="transport.task.submit@v1",
                operation_id=row["operation_id"],
                owner_domain=row["owner_domain"],
                created_at=row["created_at"],
            )
        )
    bodies = (
        await read_bodies(session, record_ids=(root.request_record_id,))
        if root.request_record_id
        else {}
    )
    request = bodies[root.request_record_id].raw_body if root.request_record_id in bodies else None
    try:
        validate_transport_exchange(vars(root), row, candidates, request_raw=request)
    except CommandError:
        raise Conflict("identity") from None
    await _associate(session, root, "transport_task", row["id"], row["created_at"])
    if root.capture_origin != "BACKFILL":
        raise Conflict("identity")
    values = {"submission_exchange_id": root.id}
    acceptance = row["submission_acceptance"]
    if acceptance:
        if acceptance.get("format_version") == 2:
            # Source snapshot excludes bindings, but a completed v2 conversion is already canonical.
            pass
        else:
            original = dict(acceptance)
            for slot, field in (
                ("request", "acceptance_request_record_id"),
                ("response", "acceptance_response_record_id"),
            ):
                raw = original.get(slot + "_raw")
                if not isinstance(raw, str):
                    raise Conflict("bytes")
                values[field] = await _record(
                    session,
                    root,
                    "transport_task",
                    row["id"],
                    version,
                    "acceptance_" + slot,
                    kind="REQUEST_REGISTERED" if slot == "request" else "HTTP_RESPONSE_OBSERVED",
                    raw=raw.encode(),
                    at=None,
                    direction="OUTBOUND" if slot == "request" else "INBOUND",
                )
    if row["applied_outcome_revision"] is not None:
        existing_fact = await session.scalar(
            select(Record).where(
                Record.exchange_id == root.id,
                Record.record_kind == "RESULT_APPLIED",
                Record.outcome_revision == row["applied_outcome_revision"],
            )
        )
        if existing_fact is not None and (
            existing_fact.effective_payload != row["applied_outcome"]
            or existing_fact.business_result != row["outcome_status"]
        ):
            raise Conflict("bytes")
        if existing_fact is None:
            await _record(
                session,
                root,
                "transport_task",
                row["id"],
                version,
                "applied",
                kind="RESULT_APPLIED",
                outcome_revision=row["applied_outcome_revision"],
                application_state="APPLIED",
                business_result=row["outcome_status"],
                effective_payload=row["applied_outcome"],
                details={"coverage_reason": "LEGACY_ORIGINAL_CALLBACK_UNKNOWN"},
            )
    await _guarded_bind(session, table, row, values, bindings=bindings._mapping)
    for kind, namespace, value in (
        ("TRANSPORT_TASK", "id", row["transport_task_id"]),
        ("WORKLINE", "code", row["workline_id"]),
    ):
        await write_reference(
            session,
            exchange_id=root.id,
            reference_kind=kind,
            namespace=namespace,
            value=value,
            relation_role="ORIGINAL_TASK",
            created_at=row["created_at"],
        )
    return root


async def _action(session, row, version):
    from app.integrations.history import SourceRef, _source_fact

    source = (
        SourceRef("ECS_COMMAND", row["command_code"])
        if row["command_code"]
        else SourceRef("TRANSPORT_TASK", row["transport_task_id"])
        if row["transport_task_id"]
        else SourceRef("WMS_MESSAGE", str(row["message_id"]))
        if row["message_id"]
        else None
    )
    root_id, _ = await _source_fact(session, source, "coarse_sorting") if source else (None, None)
    root = await session.get(Exchange, root_id) if root_id else None
    if root is None:
        root = await _root(
            session,
            "coarse_sorting_action",
            row,
            role="DIAGNOSTIC",
            protocol="legacy-consumption",
            peer_system=None,
            direction="INBOUND",
            interaction_kind="CALLBACK",
            identity_kind="BUSINESS_ACTION",
            identity_value=row["id"],
            created_at=row["created_at"],
        )
    else:
        await _associate(session, root, "coarse_sorting_action", row["id"], row["created_at"])
    for ordinal, evidence in enumerate(row["evidence_history"] or []):
        consumed = evidence.get("consumed_at")
        at = datetime.fromisoformat(consumed) if isinstance(consumed, str) else None
        parent = None
        if source is not None:
            revision = (
                evidence.get("digest")
                if source.kind == "ECS_COMMAND"
                else evidence.get("outcome_revision")
            )
            proven_root, candidate = await _source_fact(
                session, SourceRef(source.kind, source.identity, revision), "coarse_sorting"
            )
            if proven_root == root.id and candidate:
                fact = await session.get(Record, candidate)
                if source.kind == "ECS_COMMAND" and fact.effective_payload == evidence.get("data"):
                    parent = candidate
                elif source.kind == "TRANSPORT_TASK" and revision is not None:
                    parent = candidate
                elif source.kind == "WMS_MESSAGE":
                    bodies = await read_bodies(session, record_ids=(candidate,))
                    raw = bodies[candidate].raw_body
                    try:
                        reply = json.loads(raw) if raw is not None else None
                    except (ValueError, UnicodeError):
                        reply = None
                    if isinstance(reply, dict) and all(
                        reply.get(k) == evidence.get(k)
                        for k in ("operation_id", "code", "timestamp", "data")
                    ):
                        parent = candidate
        # Original saved consumption is retained, but no timestamp or source version is invented.
        await _record(
            session,
            root,
            "coarse_sorting_action",
            row["id"],
            version,
            f"consumed-{ordinal}",
            kind="BUSINESS_CONSUMED",
            parent_record_id=parent,
            at=at,
            effective_payload=evidence,
            application_state="RECORDED_CONSUMPTION",
            details={
                "owner_domain": "coarse_sorting",
                "action_id": row["id"],
                "coverage_reason": "LEGACY_SAVED_CONSUMPTION",
            },
        )
    for kind, value in (
        ("BUSINESS_ACTION", row["id"]),
        ("BUSINESS_TASK", row["job_id"]),
        ("BUSINESS_PREPARATION", row["preparation_id"]),
    ):
        if value:
            await write_reference(
                session,
                exchange_id=root.id,
                reference_kind=kind,
                namespace="coarse_sorting",
                value=value,
                relation_role="ORIGINAL_OWNER",
                created_at=row["created_at"],
            )
    return root


MAPPERS = {
    "ecs_command": _command,
    "callback_receipt": _receipt,
    "reliable_message": _message,
    "ecs_send_evidence": _send,
    "ecs_result_evidence": _result,
    "callback_snapshot": _snapshot,
    "transport_task": _transport,
    "coarse_sorting_action": _action,
}


async def backfill_batch(
    session, *, source: str, after: int | str | None, limit: int
) -> BatchResult:
    revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != "integrationhistoryexpand":
        raise ValueError("backfill requires the expand phase; new authority must remain untouched")
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("batch size must be 1..5000")
    table = await source_table(session, source)
    pk = table.c.command_code if source == "ecs_command" else table.c.id
    statement = (
        select(*(table.c[name] for name in SOURCES[source].split())).order_by(pk).limit(limit)
    )
    if after is not None:
        statement = statement.where(pk > after)
    rows = (await session.execute(statement)).mappings().all()
    copied = reused = conflicts = 0
    for row in rows:
        row = dict(row)
        key = row[pk.name]
        version = snapshot_version(row)
        marker = await session.scalar(
            select(Record.id).where(
                Record.source_kind == source,
                Record.source_key == str(key),
                Record.source_version == version,
            )
        )
        try:
            async with session.begin_nested():
                result = await MAPPERS[source](session, row, version)
                root, errors = result if isinstance(result, tuple) else (result, 0)
                await write_record(
                    session,
                    draft=RecordDraft(
                        exchange_id=root.id,
                        record_kind="LEGACY_SNAPSHOT",
                        event_key=f"SOURCE:{source}:{key}:{version}",
                        direction="INTERNAL",
                        capture_origin="BACKFILL",
                        source_kind=source,
                        source_key=str(key),
                        source_version=version,
                        http_status=row["last_http_status"]
                        if source == "reliable_message"
                        else None,
                        protocol_verdict=row["last_verdict"]
                        if source == "reliable_message"
                        else None,
                        details={
                            "attempt_count": row["attempts"],
                            "recorded_delivery_state": row["status"],
                        }
                        if source == "reliable_message"
                        else None,
                    ),
                )
                conflicts += errors
                session.info.setdefault("history_conflicts", []).extend(["bytes"] * errors)
            copied += marker is None
            reused += marker is not None
        except Conflict as error:
            session.info.setdefault("history_conflicts", []).append(str(error))
            conflicts += 1
    return BatchResult(
        source, rows[-1][pk.name] if rows else after, copied, reused, conflicts, len(rows) < limit
    )


async def _sequence(session):
    highest = await session.scalar(select(func.max(Exchange.receipt_id))) or 0

    def ids(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key
                    in {"event_id", "canonical_event_id", "predecessor_event_id", "canonical_id"}
                    and type(item) is int
                ):
                    yield item
                yield from ids(item)
        elif isinstance(value, list):
            for item in value:
                yield from ids(item)

    for row in await session.execute(select(Exchange.application_ref, Exchange.event_context)):
        highest = max([highest, *ids(row.application_ref), *ids(row.event_context)])
    for table_name, columns in {
        "bin_roller_bin_passage": ("scan1_event_id", "scan2_event_id"),
        "bin_roller_bin_return": ("scan3_event_id", "scan4_event_id", "first_scan4_event_id"),
        "coarse_sorting_job": ("receipt_id",),
    }.items():
        for column in columns:
            highest = max(
                highest, await session.scalar(text(f"SELECT max({column}) FROM {table_name}")) or 0
            )
    if session.bind.dialect.name == "postgresql":
        for name in ("callback_receipt_id_seq", "integration_receipt_id_seq"):
            value = (await session.execute(text(f"SELECT last_value,is_called FROM {name}"))).one()
            highest = max(highest, value.last_value if value.is_called else value.last_value - 1)
        await session.execute(
            text("SELECT setval('integration_receipt_id_seq', :high, true)"),
            {"high": max(1, highest)},
        )
    else:
        value = (
            await session.scalar(
                text("SELECT seq FROM sqlite_sequence WHERE name='_integration_receipt_sequence'")
            )
            or 0
        )
        highest = max(highest, value)
        if highest:
            await session.execute(
                text("INSERT OR IGNORE INTO _integration_receipt_sequence(id) VALUES (:high)"),
                {"high": highest},
            )
    return highest


async def reconcile(
    session, *, phase: Literal["precopy", "final"], writer_evidence: WriterEvidence | None
) -> ReconciliationReport:
    if phase not in {"precopy", "final"}:
        raise ValueError("invalid reconciliation phase")
    status = writer_evidence.status if writer_evidence else "UNKNOWN"
    if session.bind.dialect.name == "postgresql":
        active = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND pid<>pg_backend_pid() AND xact_start IS NOT NULL"
            )
        )
        if active:
            status = "RUNNING"
        elif phase == "final" and status == "DRAINED":
            for source in SOURCES:
                await session.execute(text(f"LOCK TABLE {source} IN SHARE MODE NOWAIT"))
    session.info["history_conflicts"] = []
    counts, conflicts = {}, 0
    # Always start at the beginning: PK checkpoints cannot detect changed old rows.
    for source in SOURCES:
        cursor, count = None, 0
        while True:
            batch = await backfill_batch(session, source=source, after=cursor, limit=500)
            count += batch.copied + batch.reused + batch.conflicts
            conflicts += batch.conflicts
            cursor = batch.next_cursor
            if batch.done:
                break
        source_relation = await source_table(session, source)
        counts[source] = await session.scalar(select(func.count()).select_from(source_relation))
    high = await _sequence(session)
    await session.flush()
    missing = await session.scalar(
        text(
            "SELECT count(*) FROM reliable_message "
            "WHERE exchange_id IS NULL OR request_record_id IS NULL"
        )
    )
    missing += await session.scalar(
        text("SELECT count(*) FROM transport_task WHERE submission_exchange_id IS NULL")
    )
    proof_errors = 0
    if not conflicts and not missing:
        from alembic.util import CommandError

        try:
            connection = await session.connection()
            await connection.run_sync(validate_originals)
        except CommandError:
            proof_errors = 1
    errors = session.info["history_conflicts"]
    identities = errors.count("identity")
    return ReconciliationReport(
        counts,
        identities,
        errors.count("bytes") + proof_errors,
        missing,
        status,
        high,
        phase == "final"
        and status == "DRAINED"
        and conflicts == 0
        and proof_errors == 0
        and missing == 0,
    )


def load_state(path):
    if not path.exists():
        return {"version": 1, "sources": {}}
    if path.stat().st_size > 16384:
        raise ValueError("checkpoint is too large")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "sources"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["sources"], dict)
        or not set(value["sources"]) <= SOURCES.keys()
    ):
        raise ValueError("invalid checkpoint")
    for source, progress in value["sources"].items():
        if not isinstance(progress, dict) or set(progress) != {
            "cursor",
            "copied",
            "reused",
            "conflicts",
            "done",
        }:
            raise ValueError("invalid source progress")
        if (
            any(
                type(progress[k]) is not int or progress[k] < 0
                for k in ("copied", "reused", "conflicts")
            )
            or type(progress["done"]) is not bool
        ):
            raise ValueError("invalid progress counts")
        cursor = progress["cursor"]
        if cursor is not None:
            if source in {"ecs_command", "coarse_sorting_action"}:
                if not isinstance(cursor, str) or str(UUID(cursor)) != cursor:
                    raise ValueError("invalid source cursor")
            elif type(cursor) is not int or not 0 < cursor < 2**63:
                raise ValueError("invalid source cursor")
    return value


def save_state(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


async def _run(args):
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is required")
    engine = create_database_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as coordinator:
            pg = coordinator.dialect.name == "postgresql"
            revision = await coordinator.scalar(text("SELECT version_num FROM alembic_version"))
            if revision in {
                "http_exchange_runtime_authority",
                "http_exchange_query_views",
                "http_exchange_initiator",
            }:
                raise ValueError("legacy integration history backfill has retired")
            if pg and not await coordinator.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            ):
                raise ValueError("another history coordinator is active")
            await coordinator.commit()
            try:
                state = load_state(args.state)
                if args.command == "copy":
                    for source in SOURCES if args.source == "all" else (args.source,):
                        cursor = state["sources"].get(source, {}).get("cursor")
                        while True:
                            async with factory() as session, session.begin():
                                if not pg:
                                    await session.execute(text("BEGIN IMMEDIATE"))
                                result = await backfill_batch(
                                    session, source=source, after=cursor, limit=args.batch_size
                                )
                            if result.conflicts:
                                print(json.dumps(asdict(result)), flush=True)
                                return 2
                            cursor = result.next_cursor
                            state["sources"][source] = {
                                "cursor": cursor,
                                **{
                                    key: value
                                    for key, value in asdict(result).items()
                                    if key not in {"source", "next_cursor"}
                                },
                            }
                            save_state(args.state, state)
                            print(json.dumps(asdict(result)), flush=True)
                            if result.conflicts:
                                return 2
                            if result.done:
                                break
                    return 0
                evidence = (
                    WriterEvidence.from_dict(json.loads(args.writer_evidence.read_text()))
                    if args.writer_evidence
                    else None
                )
                async with factory() as session, session.begin():
                    if not pg:
                        await session.execute(text("BEGIN IMMEDIATE"))
                    report = await reconcile(session, phase=args.phase, writer_evidence=evidence)
                print(json.dumps(asdict(report), sort_keys=True))
                return 0 if report.eligible else 2
            finally:
                if pg:
                    await coordinator.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY}
                    )
    finally:
        await engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("copy", "verify"))
    parser.add_argument("--source", choices=("all", *SOURCES), default="all")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--phase", choices=("precopy", "final"), default="precopy")
    parser.add_argument("--writer-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception:
        # Original exceptions may include SQL, credentials, or raw bodies.
        print(json.dumps({"error": "HISTORY_MIGRATION_FAILED", "sources_retained": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
