"""Frozen original-storage proof for integrationhistoryauthority/contract.

Keep this version immutable after release; a later format requires another version.
"""

import base64
import binascii
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from alembic.util import CommandError

# Frozen source contract for this revision; never import a future application mapper.
SOURCES = {
    "ecs_command": (
        "command_code owner_domain call_key call_key_digest input_raw device_code task_type "
        "route request_raw target delivery_snapshot expires_at status delivery_status "
        "dispatch_started_at ack_raw hold_reason blocked needs_reconciliation "
        "applied_result_id created_at"
    ),
    "callback_receipt": (
        "id operation operation_id device_code raw_digest effective_digest compare_mode "
        "status_code body resp_timestamp created_at event_envelope event_context "
        "application_ref diagnostic"
    ),
    "reliable_message": (
        "id target operation operation_id delivery_snapshot pause_reason request_raw "
        "response_raw status fail_reason attempts next_attempt_at not_received_count "
        "delivery_uncertain last_attempt_at last_http_status last_failure_kind last_verdict "
        "last_response_raw lease_id lease_until scope_owner scope_kind scope_id "
        "dispatch_started_at business_key expires_at trace_id created_at updated_at"
    ),
    "ecs_send_evidence": (
        "id command_code lease_id outcome response_raw http_status late created_at"
    ),
    "ecs_result_evidence": ("id command_code digest core status_code response applied created_at"),
    "callback_snapshot": (
        "id request_id received_at body_b64 truncated http_status response_body outcome source"
    ),
    "transport_task": (
        "id client_request_id transport_task_id operation_id kind input_digest workline_id "
        "station_id owner_domain created_at cycle_id frozen submission_acceptance "
        "member_positions outcome_status applied_outcome applied_outcome_revision "
        "outcome_digests pending_outcome position_observation"
    ),
    "coarse_sorting_action": (
        "id job_id preparation_id operation operation_id message_id command_owner call_key "
        "command_code device_code transport_task_id evidence_history created_at "
        "last_consumed_at"
    ),
}


def snapshot_version(value):
    def normalize(item):
        if isinstance(item, UUID):
            return item.hex
        if isinstance(item, bytes):
            return {"bytes": len(item), "sha256": sha256(item).hexdigest()}
        if isinstance(item, datetime):
            return (
                item.replace(tzinfo=UTC).isoformat()
                if item.tzinfo is None
                else item.astimezone(UTC).isoformat()
            )
        if isinstance(item, dict):
            return {key: normalize(v) for key, v in item.items()}
        if isinstance(item, (tuple, list)):
            return [normalize(v) for v in item]
        return item

    return sha256(
        json.dumps(
            normalize(dict(value)),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def transport_submission_candidates(message, task):
    statement = sa.select(message).where(
        message.c.operation == "transport.task.submit@v1",
        message.c.operation_id == task["operation_id"],
        message.c.business_key == task["transport_task_id"],
    )
    acceptance = task["submission_acceptance"]
    if acceptance is not None:
        if not isinstance(acceptance, dict) or any(
            acceptance.get(key) != value
            for key, value in (
                ("operation", "transport.task.submit@v1"),
                ("operation_id", task["operation_id"]),
                ("business_key", task["transport_task_id"]),
            )
        ):
            raise CommandError("refusing transport submission identity mismatch")
        if (
            type(acceptance.get("message_id")) is not int
            or acceptance["message_id"] <= 0
            or not isinstance(acceptance.get("target"), str)
            or not acceptance["target"]
        ):
            raise CommandError("refusing transport submission identity mismatch")
        statement = sa.select(message).where(message.c.id == acceptance["message_id"])
    return statement.limit(2)


def validate_transport_exchange(exchange, task, candidates, *, request_raw=None, retired=False):
    def refuse():
        raise CommandError("refusing transport submission identity mismatch")

    if len(candidates) > 1 or any(
        exchange[key] != value
        for key, value in (
            ("role", "OUTGOING"),
            ("direction", "OUTBOUND"),
            ("interaction_kind", "REQUEST"),
            ("operation", "transport.task.submit@v1"),
            ("operation_id", task["operation_id"]),
        )
    ):
        refuse()
    if exchange["identity_kind"] == "TRANSPORT_TASK_ID":
        if (
            candidates
            or exchange["identity_value"] != task["transport_task_id"]
            or exchange["protocol"] != "wms-transport"
            or exchange["owner_domain"] != task["owner_domain"]
        ):
            refuse()
        return
    if (
        exchange["identity_kind"] != "OPERATION_ID"
        or exchange["identity_value"] != task["operation_id"]
    ):
        refuse()
    if candidates:
        original = candidates[0]
        if any(
            original[key] != value
            for key, value in (
                ("exchange_id", exchange["id"]),
                ("target", exchange["target_key"]),
                ("operation", "transport.task.submit@v1"),
                ("operation_id", task["operation_id"]),
                ("business_key", task["transport_task_id"]),
            )
        ):
            refuse()
    elif not retired:
        refuse()
    acceptance = task["submission_acceptance"]
    if acceptance and acceptance.get("target") != exchange["target_key"]:
        refuse()
    try:
        envelope = json.loads(request_raw)
        if (
            envelope["operation"] != "transport.task.submit@v1"
            or envelope["operation_id"] != task["operation_id"]
            or envelope["data"]["transport_task_id"] != task["transport_task_id"]
        ):
            refuse()
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError):
        refuse()


def validate_originals(bind):
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

    def refuse():
        raise CommandError(
            "refusing authority preparation: original identity/version/body mismatch"
        )

    def root(identity):
        value = (
            bind.execute(sa.select(roots).where(roots.c.id == identity)).mappings().one_or_none()
        )
        if value is None:
            refuse()
        return value

    def record(identity):
        value = (
            bind.execute(sa.select(records).where(records.c.id == identity))
            .mappings()
            .one_or_none()
        )
        if value is None:
            refuse()
        return value

    def equal(left, right):
        if isinstance(left, bytes) or isinstance(right, bytes):
            if left != right:
                refuse()
            return
        if snapshot_version({"value": left}) != snapshot_version({"value": right}):
            refuse()

    def body(identity, exchange_id, expected, *, unavailable=False):
        if expected is None and not unavailable:
            if identity is not None:
                refuse()
            return
        item = record(identity)
        if item["exchange_id"] != exchange_id or item["evidence_class"] != "CORE":
            refuse()
        if expected is None and item["body_record_id"] is not None:
            refuse()
        if item["body_record_id"]:
            item = record(item["body_record_id"])
            if (
                item["exchange_id"] != exchange_id
                or item["evidence_class"] != "CORE"
                or item["body_record_id"]
            ):
                refuse()
        raw = item["raw_body"]
        if expected is None:
            if item["body_state"] != "UNAVAILABLE" or any(
                item[field] is not None
                for field in ("raw_body", "body_sha256", "stored_bytes", "body_record_id")
            ):
                refuse()
            return
        if (
            raw is None
            or bytes(raw) != expected
            or item["body_sha256"] != sha256(expected).hexdigest()
            or item["stored_bytes"] != len(expected)
            or item["body_state"] not in {"FULL", "LEGACY_REENCODED"}
        ):
            refuse()
        return bytes(raw)

    def reply(value):
        return (
            b""
            if value is None
            else json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
        )

    for source, columns in SOURCES.items():
        original = table(source)
        for row in bind.execute(
            sa.select(*(original.c[name] for name in columns.split()))
        ).mappings():
            key = row["command_code"] if source == "ecs_command" else row["id"]
            version = snapshot_version(row)
            marker = bind.execute(
                sa.select(
                    records.c.exchange_id,
                    records.c.metadata.label("details"),
                    records.c.http_status,
                    records.c.protocol_verdict,
                ).where(
                    records.c.source_kind == source,
                    records.c.source_key == str(key),
                    records.c.source_version == version,
                )
            ).one_or_none()
            if marker is None:
                refuse()
            exchange = root(marker.exchange_id)
            if exchange["capture_origin"] != "BACKFILL":
                refuse()
            if source == "ecs_command":
                equal(exchange["role"], "ECS_COMMAND")
                for field in columns.split():
                    if field not in {"request_raw", "ack_raw"}:
                        equal(exchange[field], row[field])
                body(exchange["request_record_id"], exchange["id"], bytes(row["request_raw"]))
                equal(exchange["request_sha256"], sha256(bytes(row["request_raw"])).hexdigest())
                body(
                    exchange["ack_record_id"],
                    exchange["id"],
                    None if row["ack_raw"] is None else bytes(row["ack_raw"]),
                )
                if row["applied_result_id"] is not None:
                    applied = bind.execute(
                        sa.select(records.c.id).where(
                            records.c.exchange_id == exchange["id"],
                            records.c.record_kind == "RESULT_EVIDENCE",
                            records.c.semantic_digest == row["applied_result_id"],
                            records.c.applied.is_(True),
                        )
                    ).one_or_none()
                    if applied is None:
                        refuse()
            elif source == "callback_receipt":
                equal(exchange["role"], "RECEIPT")
                equal(exchange["receipt_id"], row["id"])
                equal(exchange["receipt_active"], True)
                for field in columns.split():
                    if field not in {"id", "body"}:
                        equal(exchange[field], row[field])
                body(exchange["current_response_record_id"], exchange["id"], reply(row["body"]))
            elif source == "reliable_message":
                equal(
                    marker.details,
                    {"attempt_count": row["attempts"], "recorded_delivery_state": row["status"]},
                )
                equal(marker.http_status, row["last_http_status"])
                equal(marker.protocol_verdict, row["last_verdict"])
                bound = bind.execute(
                    sa.select(original.c.exchange_id).where(original.c.id == row["id"])
                ).scalar_one()
                equal(bound, exchange["id"])
                if row["operation"] == "ecs.command":
                    equal(exchange["command_code"], row["operation_id"])
                    equal(exchange["target"], row["target"])
                    equal(
                        bind.execute(
                            sa.select(original.c.request_record_id).where(
                                original.c.id == row["id"]
                            )
                        ).scalar_one(),
                        exchange["request_record_id"],
                    )
                    body(exchange["request_record_id"], exchange["id"], bytes(row["request_raw"]))
                else:
                    equal(exchange["operation"], row["operation"])
                    equal(exchange["operation_id"], row["operation_id"])
                    equal(exchange["target_key"], row["target"])
            elif source == "ecs_result_evidence":
                equal(exchange["command_code"], row["command_code"])
                result = (
                    bind.execute(
                        sa.select(records).where(
                            records.c.exchange_id == exchange["id"],
                            records.c.semantic_digest == row["digest"],
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if result is None:
                    refuse()
                for actual, expected in (
                    (result["effective_payload"], row["core"]),
                    (result["applied"], row["applied"]),
                    (result["http_status"], row["status_code"]),
                    (result["occurred_at"], row["created_at"]),
                ):
                    equal(actual, expected)
                body(result["response_record_id"], exchange["id"], reply(row["response"]))
                body(result["id"], exchange["id"], reply(row["core"]))
            elif source == "ecs_send_evidence":
                equal(exchange["command_code"], row["command_code"])
                evidence = (
                    bind.execute(
                        sa.select(records).where(
                            records.c.source_kind == "ecs_send_evidence:conclusion",
                            records.c.source_key == str(key),
                            records.c.source_version == version,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if evidence is None:
                    refuse()
                equal(evidence["attempt_id"], row["lease_id"])
                if row["outcome"] != "OPEN_ATTEMPT":
                    equal(evidence["ecs_attempt_outcome"], row["outcome"])
                    equal(evidence["http_status"], row["http_status"])
                    equal(evidence["late"], row["late"])
                    body(
                        evidence["id"],
                        exchange["id"],
                        None if row["response_raw"] is None else bytes(row["response_raw"]),
                        unavailable=row["response_raw"] is None,
                    )
                    latest = bind.execute(
                        sa.select(records.c.ecs_attempt_outcome)
                        .where(
                            records.c.exchange_id == exchange["id"],
                            records.c.attempt_id == row["lease_id"],
                            records.c.record_kind == "ECS_ATTEMPT_CONCLUSION",
                        )
                        .order_by(records.c.attempt_revision.desc())
                        .limit(1)
                    ).scalar_one()
                    equal(latest, row["outcome"])
            elif source == "callback_snapshot":
                try:
                    decoded = base64.b64decode(row["body_b64"], validate=True)
                except (ValueError, binascii.Error):
                    refuse()
                evidence = (
                    bind.execute(
                        sa.select(records).where(
                            records.c.source_kind == "callback_snapshot:request",
                            records.c.source_key == str(key),
                            records.c.source_version == version,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if evidence is None:
                    refuse()
                equal(evidence["raw_body"], decoded)
                equal(evidence["body_state"], "PREVIEW")
                equal(evidence["body_sha256"], sha256(decoded).hexdigest())
                equal(evidence["stored_bytes"], len(decoded))
                equal(exchange["created_at"], row["received_at"])
            elif source == "transport_task":
                current = (
                    bind.execute(sa.select(original).where(original.c.id == key)).mappings().one()
                )
                equal(current["submission_exchange_id"], exchange["id"])
                equal(exchange["operation_id"], row["operation_id"])
                candidates = (
                    bind.execute(transport_submission_candidates(table("reliable_message"), row))
                    .mappings()
                    .all()
                )
                request = None
                if exchange["request_record_id"] is not None:
                    saved = record(exchange["request_record_id"])
                    if saved["body_record_id"]:
                        saved = record(saved["body_record_id"])
                    request = body(
                        exchange["request_record_id"], exchange["id"], bytes(saved["raw_body"])
                    )
                validate_transport_exchange(exchange, row, candidates, request_raw=request)
                acceptance = row["submission_acceptance"]
                if acceptance:
                    if "request_raw" not in acceptance or "response_raw" not in acceptance:
                        refuse()
                    body(
                        current["acceptance_request_record_id"],
                        exchange["id"],
                        acceptance["request_raw"].encode(),
                    )
                    body(
                        current["acceptance_response_record_id"],
                        exchange["id"],
                        acceptance["response_raw"].encode(),
                    )
                    proof = dict(acceptance)
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
                if row["applied_outcome_revision"] is not None:
                    saved = (
                        bind.execute(
                            sa.select(records.c.effective_payload).where(
                                records.c.exchange_id == exchange["id"],
                                records.c.record_kind == "RESULT_APPLIED",
                                records.c.outcome_revision == row["applied_outcome_revision"],
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(saved) != 1:
                        refuse()
                    equal(saved[0], row["applied_outcome"])
            elif source == "coarse_sorting_action":
                for ordinal, evidence in enumerate(row["evidence_history"] or []):
                    saved = bind.execute(
                        sa.select(records.c.effective_payload).where(
                            records.c.source_kind == f"coarse_sorting_action:consumed-{ordinal}",
                            records.c.source_key == str(key),
                            records.c.source_version == version,
                        )
                    ).scalar_one_or_none()
                    equal(saved, evidence)
    if bind.dialect.name == "postgresql":
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
