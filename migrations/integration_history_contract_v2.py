"""Frozen contraction proof; v1 remains the immutable authority-cutover proof."""

import base64
import binascii
import json
import re
from hashlib import sha256
from pathlib import Path

import sqlalchemy as sa
from alembic.util import CommandError

from migrations.integration_history_v1 import (
    SOURCES,
    snapshot_version,
    transport_submission_candidates,
    validate_transport_exchange,
)

RETIRED = (
    "ecs_send_evidence",
    "ecs_result_evidence",
    "ecs_command",
    "callback_snapshot",
    "callback_receipt",
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


def refuse():
    raise CommandError(
        "refusing integration history contraction: evidence or maintenance gate mismatch"
    )


def maintenance_gate(bind, path, *, populated):
    if populated:
        try:
            content = Path(path).read_bytes()
            if len(content) > 4096:
                refuse()
            evidence = json.loads(content)
            if set(evidence) != set(WRITERS) | {"attestation", "authority_acceptance"}:
                refuse()
            if any(evidence[name] != "DRAINED" for name in WRITERS):
                refuse()
            if any(
                not isinstance(evidence[name], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", evidence[name])
                for name in ("attestation", "authority_acceptance")
            ):
                refuse()
        except (TypeError, ValueError, OSError, RecursionError):
            refuse()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    inspector = sa.inspect(bind)
    for current_schema in inspector.get_schema_names() if schema else (None,):
        if current_schema in {"information_schema", "pg_catalog"}:
            continue
        for name in inspector.get_table_names(schema=current_schema):
            for fk in inspector.get_foreign_keys(name, schema=current_schema):
                if fk["referred_table"] in RETIRED and fk.get(
                    "referred_schema", current_schema
                ) in {schema, None}:
                    refuse()
    if schema:
        if bind.scalar(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                "AND xact_start IS NOT NULL)"
            )
        ):
            refuse()
        for name in (
            *SOURCES,
            "integration_exchange",
            "integration_record",
            "integration_reference",
        ):
            bind.execute(
                sa.text(f'LOCK TABLE "enterprise_backend"."{name}" IN ACCESS EXCLUSIVE MODE NOWAIT')
            )


def validate_contract(bind):
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    metadata = sa.MetaData()
    tables = {}

    def table(name):
        if name not in tables:
            tables[name] = sa.Table(
                name, metadata, schema=schema, autoload_with=bind, resolve_fks=False
            )
        return tables[name]

    roots, records = table("integration_exchange"), table("integration_record")

    def one(statement):
        row = bind.execute(statement).mappings().one_or_none()
        if row is None:
            refuse()
        return row

    def equal(left, right):
        if snapshot_version({"value": left}) != snapshot_version({"value": right}):
            refuse()

    def root(identity):
        return one(sa.select(roots).where(roots.c.id == identity))

    def record(identity):
        return one(sa.select(records).where(records.c.id == identity))

    def body(identity, exchange_id, expected=None, *, purged=False, core=True, unavailable=False):
        item = record(identity)
        equal(item["exchange_id"], exchange_id)
        if core and item["evidence_class"] != "CORE":
            refuse()
        original = item
        if item["body_record_id"]:
            item = record(item["body_record_id"])
            if item["body_record_id"] or (core and item["evidence_class"] != "CORE"):
                refuse()
        raw = item["raw_body"]
        if unavailable and item["body_state"] == "UNAVAILABLE":
            if (
                original["body_record_id"] is not None
                or item["record_kind"] != "SEND_ATTEMPT_FINISHED"
            ) or any(
                item[field] is not None
                for field in ("raw_body", "body_sha256", "stored_bytes", "body_record_id")
            ):
                refuse()
            return None
        if purged and item["body_state"] == "PURGED":
            if raw is not None or not (item["metadata"] or {}).get("purged_at"):
                refuse()
            if expected is not None:
                equal(item["body_sha256"], sha256(expected).hexdigest())
                equal(item["stored_bytes"], len(expected))
            return None
        if raw is None or item["body_state"] not in {"FULL", "PREVIEW", "LEGACY_REENCODED"}:
            refuse()
        raw = bytes(raw)
        equal(item["body_sha256"], sha256(raw).hexdigest())
        equal(item["stored_bytes"], len(raw))
        if expected is not None:
            equal(raw, expected)
        return raw

    def reply(value):
        return (
            b""
            if value is None
            else json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        )

    def source_record(source, key, version, slot):
        return one(
            sa.select(records).where(
                records.c.source_kind == source + ":" + slot,
                records.c.source_key == str(key),
                records.c.source_version == version,
            )
        )

    def baseline(row, source):
        key = row["command_code"] if source == "ecs_command" else row["id"]
        version = snapshot_version({name: row[name] for name in SOURCES[source].split()})
        marker = one(
            sa.select(records).where(
                records.c.source_kind == source,
                records.c.source_key == str(key),
                records.c.source_version == version,
            )
        )
        exchange = root(marker["exchange_id"])
        if exchange["capture_origin"] != "BACKFILL":
            refuse()
        return key, version, exchange

    for source in RETIRED:
        original = table(source)
        pk = original.c.command_code if source == "ecs_command" else original.c.id
        missing = bind.execute(
            sa.select(records).where(
                records.c.source_kind == source,
                ~sa.exists(sa.select(1).where(sa.cast(pk, sa.Text) == records.c.source_key)),
            )
        ).mappings()
        for marker in missing:
            if source != "callback_snapshot":
                refuse()
            saved = source_record(source, marker["source_key"], marker["source_version"], "request")
            if saved["body_state"] != "PURGED":
                refuse()
            body(saved["id"], marker["exchange_id"], purged=True, core=False)
    # Retired sources cannot change after authority handoff; their exact baseline must survive.
    for source in RETIRED:
        for row in bind.execute(sa.select(table(source))).mappings():
            key, version, exchange = baseline(row, source)
            identity = exchange["id"]
            if source == "ecs_command":
                equal(exchange["role"], "ECS_COMMAND")
                for field in (
                    "command_code",
                    "owner_domain",
                    "call_key",
                    "call_key_digest",
                    "input_raw",
                    "device_code",
                    "task_type",
                    "route",
                    "request_sha256",
                    "target",
                    "delivery_snapshot",
                    "expires_at",
                    "created_at",
                ):
                    equal(
                        exchange[field],
                        sha256(bytes(row["request_raw"])).hexdigest()
                        if field == "request_sha256"
                        else row[field],
                    )
                body(exchange["request_record_id"], identity, bytes(row["request_raw"]))
                if row["ack_raw"] is not None:
                    saved = source_record(source, key, version, "ack")
                    body(saved["id"], identity, bytes(row["ack_raw"]))
            elif source == "callback_receipt":
                equal(exchange["role"], "RECEIPT")
                equal(exchange["receipt_id"], row["id"])
                for field in (
                    "operation",
                    "operation_id",
                    "raw_digest",
                    "effective_digest",
                    "compare_mode",
                    "created_at",
                ):
                    equal(exchange[field], row[field])
                saved = source_record(source, key, version, "reply")
                body(
                    saved["id"], identity, reply(row["body"]), purged=not exchange["receipt_active"]
                )
            elif source == "ecs_result_evidence":
                equal(exchange["command_code"], row["command_code"])
                saved = one(
                    sa.select(records).where(
                        records.c.exchange_id == identity,
                        records.c.record_kind == "RESULT_EVIDENCE",
                        records.c.semantic_digest == row["digest"],
                    )
                )
                for actual, original in (
                    ("effective_payload", "core"),
                    ("http_status", "status_code"),
                    ("applied", "applied"),
                    ("occurred_at", "created_at"),
                ):
                    equal(saved[actual], row[original])
                body(saved["id"], identity, reply(row["core"]))
                body(saved["response_record_id"], identity, reply(row["response"]))
            elif source == "ecs_send_evidence":
                equal(exchange["command_code"], row["command_code"])
                if row["outcome"] != "OPEN_ATTEMPT":
                    saved = source_record(source, key, version, "conclusion")
                    for actual, original in (
                        ("attempt_id", "lease_id"),
                        ("ecs_attempt_outcome", "outcome"),
                        ("http_status", "http_status"),
                        ("late", "late"),
                    ):
                        equal(saved[actual], row[original])
                    if row["response_raw"] is not None:
                        body(saved["id"], identity, bytes(row["response_raw"]))
            else:
                try:
                    raw = base64.b64decode(row["body_b64"], validate=True)
                except (ValueError, binascii.Error):
                    refuse()
                saved = source_record(source, key, version, "request")
                body(saved["id"], identity, raw, purged=True, core=False)
                equal(exchange["created_at"], row["received_at"])
    # A-side progress is authoritative; only its original immutable input stays frozen.
    for row in bind.execute(sa.select(table("reliable_message"))).mappings():
        exchange = root(row["exchange_id"])
        identity = exchange["id"]
        request = body(row["request_record_id"], identity)
        if exchange["role"] == "ECS_COMMAND":
            equal(row["request_record_id"], exchange["request_record_id"])
            for field, original in (
                ("command_code", "operation_id"),
                ("target", "target"),
                ("delivery_snapshot", "delivery_snapshot"),
                ("expires_at", "expires_at"),
            ):
                equal(exchange[field], row[original])
            equal(exchange["request_sha256"], sha256(request).hexdigest())
        else:
            for field, original in (
                ("operation", "operation"),
                ("operation_id", "operation_id"),
                ("target_key", "target"),
            ):
                equal(exchange[field], row[original])
        for column in ("terminal_response_record_id", "last_response_record_id"):
            if row[column] is not None:
                body(row[column], identity, unavailable=column == "last_response_record_id")
        for slot, column in (
            ("request", "request_raw"),
            ("terminal", "response_raw"),
            ("last", "last_response_raw"),
        ):
            if row[column] is None:
                continue
            evidence = list(
                bind.execute(
                    sa.select(records).where(
                        records.c.exchange_id == identity,
                        records.c.source_kind == "reliable_message:" + slot,
                        records.c.source_key == str(row["id"]),
                    )
                ).mappings()
            )
            if not evidence:
                refuse()
            for saved in evidence:
                body(saved["id"], identity, bytes(row[column]))
    for exchange in bind.execute(sa.select(roots)).mappings():
        identity = exchange["id"]
        for column in ("request_record_id", "ack_record_id", "current_response_record_id"):
            if exchange[column] is not None:
                body(
                    exchange[column],
                    identity,
                    purged=exchange["role"] != "ECS_COMMAND" and not exchange["receipt_active"],
                )
        if exchange["applied_result_id"] is not None:
            applied = one(
                sa.select(records).where(
                    records.c.exchange_id == identity,
                    records.c.record_kind == "RESULT_EVIDENCE",
                    records.c.semantic_digest == exchange["applied_result_id"],
                    records.c.applied.is_(True),
                )
            )
            body(applied["id"], identity)
            body(applied["response_record_id"], identity)
    normalized = []
    task = table("transport_task")
    for row in bind.execute(sa.select(task)).mappings():
        exchange = root(row["submission_exchange_id"])
        equal(exchange["operation_id"], row["operation_id"])
        candidates = (
            bind.execute(transport_submission_candidates(table("reliable_message"), row))
            .mappings()
            .all()
        )
        request = (
            body(exchange["request_record_id"], exchange["id"])
            if exchange["request_record_id"]
            else None
        )
        validate_transport_exchange(exchange, row, candidates, request_raw=request, retired=True)
        value = row["submission_acceptance"]
        if value is not None:
            if not isinstance(value, dict) or value.get("format_version") not in {None, 2}:
                refuse()
            proof = {k: v for k, v in value.items() if k != "format_version"}
            for slot, column in (
                ("request", "acceptance_request_record_id"),
                ("response", "acceptance_response_record_id"),
            ):
                raw = body(row[column], exchange["id"])
                try:
                    decoded = raw.decode()
                except UnicodeError:
                    refuse()
                if slot + "_raw" in proof:
                    equal(proof[slot + "_raw"], decoded)
                proof[slot + "_raw"] = decoded
            digest = proof.pop("evidence_sha256", None)
            equal(
                digest,
                sha256(
                    json.dumps(
                        proof,
                        sort_keys=True,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest(),
            )
            for field in (
                "owner_domain",
                "cycle_id",
                "kind",
                "transport_task_id",
                "client_request_id",
                "operation_id",
            ):
                equal(proof[field], row[field])
            safe = {k: v for k, v in value.items() if k not in {"request_raw", "response_raw"}}
            safe["format_version"] = 2
            normalized.append((row["id"], safe))
        if row["applied_outcome_revision"] is not None:
            applied = one(
                sa.select(records).where(
                    records.c.exchange_id == exchange["id"],
                    records.c.record_kind == "RESULT_APPLIED",
                    records.c.outcome_revision == row["applied_outcome_revision"],
                )
            )
            equal(applied["effective_payload"], row["applied_outcome"])
    if schema:
        old = bind.execute(
            sa.text("SELECT last_value,is_called FROM callback_receipt_id_seq")
        ).one()
        new = bind.execute(
            sa.text("SELECT last_value,is_called FROM integration_receipt_id_seq")
        ).one()
        if (new.last_value if new.is_called else new.last_value - 1) < (
            old.last_value if old.is_called else old.last_value - 1
        ):
            refuse()
    return task, normalized
