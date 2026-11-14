"""Frozen source checks for the HTTP exchange runtime authority migration."""

import json
from hashlib import sha256
from uuid import UUID

import sqlalchemy as sa
from alembic.util import CommandError

from app.strict_json import json_object
from migrations.integration_history_v1 import SOURCES, snapshot_version


def mismatch(kind, identity):
    raise CommandError(f"runtime reconciliation failed: {kind}, original identity {identity}")


def digest(value):
    return sha256(value).hexdigest()


def valid_sha(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def canonical_digest(value):
    return digest(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    )


def reconcile(bind, schema):
    metadata = sa.MetaData(schema=schema)

    def table(name):
        return sa.Table(name, metadata, autoload_with=bind, resolve_fks=False)

    roots, records, refs, tasks = map(
        table,
        ("integration_exchange", "integration_record", "integration_reference", "transport_task"),
    )
    receipts, commands, sends, results, holds = map(
        table,
        (
            "callback_receipt",
            "ecs_command",
            "ecs_send_evidence",
            "ecs_result_evidence",
            "integration_runtime_retention_hold",
        ),
    )
    audit = tuple(map(table, ("audit_logs", "audit_logs_archive")))

    def record(identity):
        return (
            bind.execute(sa.select(records).where(records.c.id == identity))
            .mappings()
            .one_or_none()
            if identity
            else None
        )

    def body(row, kind, identity, *, required=True, full_only=False):
        source = record(row["body_record_id"]) if row and row["body_record_id"] else row
        if source is None or source["raw_body"] is None:
            if required:
                mismatch(kind + " missing body", identity)
            return None
        wire = source["raw_body"]
        if (
            source["body_record_id"] is not None
            or source["stored_bytes"] != len(wire)
            or source["body_sha256"] != digest(wire)
            or (required and source["body_state"] not in {"FULL", "LEGACY_REENCODED"})
            or (full_only and source["body_state"] != "FULL")
        ):
            mismatch(kind + " body conflict", identity)
        return wire

    def reply(row, kind, identity, *, status, root):
        if (
            row is None
            or row["exchange_id"] != root
            or row["record_kind"] != "RESPONSE_PREPARED"
            or row["http_status"] != status
        ):
            mismatch(kind + " reply identity/status", identity)
        wire = body(row, kind, identity)
        try:
            document = json.loads(wire)
        except (ValueError, UnicodeError, RecursionError):
            mismatch(kind + " invalid reply", identity)
        if not isinstance(document, dict):
            mismatch(kind + " invalid reply", identity)
        return wire, document

    for root in bind.execute(sa.select(roots).where(roots.c.role == "RECEIPT")).mappings():
        identity = root["receipt_id"]
        status = root["status_code"]
        active = root["receipt_active"]
        if identity is None or identity < 1 or not valid_sha(root["raw_digest"]):
            mismatch("receipt identity/digest", identity)
        if root["compare_mode"] == "EFFECTIVE" and not valid_sha(root["effective_digest"]):
            mismatch("receipt effective digest", identity)
        wire = attempt = None
        if active:
            response = record(root["current_response_record_id"])
            wire, document = reply(response, "receipt", identity, status=status, root=root["id"])
            if root["protocol"] == "wms-callback" and (
                document.get("operation_id") != root["operation_id"]
                or document.get("timestamp") != root["resp_timestamp"]
            ):
                mismatch("receipt reply envelope", identity)
            attempt = response["attempt_id"]
        values = {
            key: root[key]
            for key in (
                "receipt_id",
                "id",
                "operation",
                "operation_id",
                "protocol",
                "owner_domain",
                "device_code",
                "raw_digest",
                "effective_digest",
                "compare_mode",
                "status_code",
                "resp_timestamp",
                "created_at",
                "event_envelope",
                "event_context",
                "application_ref",
                "diagnostic",
            )
        }
        if not active:
            # Inactive receipts may have had their protocol bytes or metadata removed by retention.
            values["status_code"] = status or 0
            values["resp_timestamp"] = root["resp_timestamp"] or 0
        bind.execute(
            receipts.insert().values(
                **values,
                active=active,
                reply_wire=wire,
                reply_sha256=digest(wire) if wire is not None else None,
                reply_attempt_id=attempt,
            )
        )
    maximum = bind.scalar(sa.select(sa.func.max(receipts.c.receipt_id))) or 0
    if schema:
        highwater, called = bind.execute(
            sa.text("SELECT last_value,is_called FROM integration_receipt_id_seq")
        ).one()
        if maximum > highwater or (maximum == highwater and not called):
            mismatch("receipt sequence high-water mark", maximum)
    elif maximum > (
        bind.scalar(
            sa.text("SELECT seq FROM sqlite_sequence WHERE name='_integration_receipt_sequence'")
        )
        or 0
    ):
        mismatch("receipt sequence high-water mark", maximum)

    for root in bind.execute(sa.select(roots).where(roots.c.role == "ECS_COMMAND")).mappings():
        code = root["command_code"]
        request = record(root["request_record_id"])
        if (
            not request
            or request["exchange_id"] != root["id"]
            or request["record_kind"] != "REQUEST_REGISTERED"
        ):
            mismatch("ECS command request identity", code)
        wire = body(request, "ECS command request", code)
        if (
            digest(wire) != root["request_sha256"]
            or digest(root["call_key"].encode()) != root["call_key_digest"]
        ):
            mismatch("ECS command digest", code)
        ack_wire = None
        if root["ack_record_id"]:
            ack_wire = _ecs_ack(
                bind, records, refs, root, request, record(root["ack_record_id"]), record, body
            )
        values = {
            key: root[key]
            for key in (
                "command_code",
                "id",
                "owner_domain",
                "call_key",
                "call_key_digest",
                "input_raw",
                "request_sha256",
                "device_code",
                "task_type",
                "route",
                "target",
                "delivery_snapshot",
                "expires_at",
                "created_at",
                "status",
                "delivery_status",
                "dispatch_started_at",
                "hold_reason",
                "blocked",
                "needs_reconciliation",
                "applied_result_id",
                "ack_record_id",
            )
        }
        bind.execute(commands.insert().values(**values, request_wire=wire, ack_wire=ack_wire))
        applied = []
        seen = set()
        evidence = (
            bind.execute(
                sa.select(records).where(
                    records.c.exchange_id == root["id"],
                    records.c.record_kind.in_(
                        ("SEND_ATTEMPT_OPENED", "ECS_ATTEMPT_CONCLUSION", "RESULT_EVIDENCE")
                    ),
                )
            )
            .mappings()
            .all()
        )
        for fact in evidence:
            if fact["record_kind"] == "RESULT_EVIDENCE":
                semantic = fact["semantic_digest"]
                core = fact["effective_payload"]
                if (
                    not isinstance(core, dict)
                    or core.get("command_code") != code
                    or core.get("device_code") != root["device_code"]
                    or digest(
                        json.dumps(
                            core, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ).encode("ascii")
                    )
                    != semantic
                ):
                    mismatch("ECS result identity/digest", code)
                if fact["applied"]:
                    applied.append(fact)
                response = record(fact["response_record_id"])
                response_wire, _ = reply(
                    response, "ECS result", code, status=fact["http_status"], root=root["id"]
                )
                receipt_id = bind.scalar(
                    sa.select(receipts.c.receipt_id).where(
                        receipts.c.operation == "ecs.result",
                        receipts.c.operation_id == semantic,
                        receipts.c.active.is_(True),
                    )
                )
                bind.execute(
                    results.insert().values(
                        command_code=code,
                        semantic_digest=semantic,
                        effective_payload=core,
                        applied=fact["applied"],
                        http_status=fact["http_status"],
                        response_wire=response_wire,
                        response_sha256=digest(response_wire),
                        attempt_id=fact["attempt_id"],
                        occurred_at=fact["occurred_at"],
                        receipt_id=receipt_id,
                    )
                )
                continue
            opened = fact["record_kind"] == "SEND_ATTEMPT_OPENED"
            lease = fact["attempt_id"]
            revision = 0 if opened else fact["attempt_revision"]
            outcome = "OPEN_ATTEMPT" if opened else fact["ecs_attempt_outcome"]
            if (
                not lease
                or revision is None
                or (not opened and revision < 1)
                or (lease, revision) in seen
                or outcome not in {"OPEN_ATTEMPT", "ACK", "NOT_RECEIVED", "REJECTED", "UNKNOWN"}
            ):
                mismatch("ECS attempt conflict", code)
            seen.add((lease, revision))
            parent = record(fact["parent_record_id"]) if not opened else None
            if parent is not None and parent["exchange_id"] != root["id"]:
                mismatch("ECS attempt response identity", code)
            if fact["parent_record_id"] and not opened and parent is None:
                mismatch("ECS attempt response missing", code)
            bind.execute(
                sends.insert().values(
                    command_code=code,
                    lease_id=lease,
                    attempt_revision=revision,
                    outcome=outcome,
                    occurred_at=fact["occurred_at"],
                    observation_id=fact["parent_record_id"] if not opened else None,
                    http_status=fact["http_status"],
                    late=fact["late"],
                    response_wire=body(parent or fact, "ECS attempt", code, required=False),
                )
            )
        current_result = root["applied_result_id"]
        if current_result is None and len(applied) == 1:
            _verify_ecs_result_reset(bind, audit, receipts, root, applied[0], record, body)
        elif [fact["semantic_digest"] for fact in applied] != (
            [current_result] if current_result else []
        ):
            mismatch("ECS applied result missing or conflicting", code)

    messages, attempts = map(table, ("reliable_message", "reliable_delivery_attempt"))
    restore_active_ecs_attempts(bind, schema, metadata, messages, records)
    for row in bind.execute(sa.select(messages)).mappings():
        identity = row["id"]
        root = bind.execute(
            sa.select(roots.c.protocol).where(roots.c.id == row["exchange_id"])
        ).one_or_none()
        if root is None:
            mismatch("message original identity", identity)
        request = record(row["request_record_id"])
        if (
            request is None
            or request["exchange_id"] != row["exchange_id"]
            or request["record_kind"] != "REQUEST_REGISTERED"
        ):
            mismatch("message frozen request identity", identity)
        wire = body(request, "message frozen request", identity, full_only=True)
        checksum = digest(wire)
        if (row["frozen_request_wire"], row["frozen_request_sha256"]) not in (
            (None, None),
            (wire, checksum),
        ):
            mismatch("message frozen request conflict", identity)
        values = {
            "frozen_request_wire": wire,
            "frozen_request_sha256": checksum,
            "protocol": root.protocol,
            "replacement_blocked": bool(
                bind.scalar(
                    sa.select(
                        sa.exists().where(
                            records.c.exchange_id == row["exchange_id"],
                            records.c.record_kind == "HTTP_RESPONSE_OBSERVED",
                            sa.or_(
                                records.c.protocol_verdict.is_(None),
                                records.c.protocol_verdict != "NOT_RECEIVED",
                            ),
                        )
                    )
                )
            ),
        }
        for slot in ("terminal", "last"):
            pointer = row[slot + "_response_record_id"]
            if pointer is None:
                continue
            fact = record(pointer)
            if fact is None or fact["exchange_id"] != row["exchange_id"]:
                mismatch("message response identity", identity)
            values[slot + "_response_wire"] = body(
                fact, "message response", identity, required=False
            )
            if slot == "terminal":
                values["terminal_response_at"] = fact["occurred_at"]
        successors = (
            bind.execute(
                sa.select(messages.c.id)
                .select_from(
                    refs.outerjoin(messages, refs.c.related_exchange_id == messages.c.exchange_id)
                )
                .where(
                    refs.c.exchange_id == row["exchange_id"],
                    refs.c.relation_role == "SUPERSEDED_BY",
                )
            )
            .scalars()
            .all()
        )
        if successors:
            if len(successors) != 1 or successors[0] is None:
                mismatch("WMS replacement identity", identity)
            values["superseded_by_message_id"] = successors[0]
        bind.execute(messages.update().where(messages.c.id == identity).values(**values))
    for attempt in bind.execute(sa.select(attempts)).mappings():
        identity = f"{attempt['message_id']}/{attempt['lease_id']}"
        request = record(attempt["request_record_id"])
        if request is None or request["exchange_id"] != attempt["exchange_id"]:
            mismatch("attempt request identity", identity)
        values = {"request_wire": body(request, "attempt request", identity)}
        if attempt["response_record_id"]:
            fact = record(attempt["response_record_id"])
            if (
                fact is None
                or fact["exchange_id"] != attempt["exchange_id"]
                or fact["attempt_id"] != attempt["lease_id"]
            ):
                mismatch("attempt response identity", identity)
            values.update(
                response_wire=body(fact, "attempt response", identity, required=False),
                response_http_status=fact["http_status"],
                response_verdict=fact["protocol_verdict"],
            )
        bind.execute(attempts.update().where(attempts.c.id == attempt["id"]).values(**values))
    _restore_static_openings(bind, messages, roots, records, attempts)
    _transport(bind, tasks, records, refs, receipts, record, body)
    _retention(bind, roots, refs, holds, messages, tasks)


def _verify_ecs_result_reset(bind, audit, receipts, root, fact, record, body):
    code = root["command_code"]
    exchange_id = str(root["id"])
    result_id = str(fact["id"])
    semantic = fact["semantic_digest"]
    candidates = sa.union_all(
        *(
            sa.select(log.c.resource, log.c.created_at, log.c.detail).where(
                log.c.action == "ops:ecs-result:reset",
                sa.or_(
                    log.c.detail["command_code"].as_string() == code,
                    log.c.detail["exchange_id"].as_string() == exchange_id,
                    log.c.detail["retained_result_record_id"].as_string() == result_id,
                ),
            )
            for log in audit
        )
    )
    rows = bind.execute(candidates.limit(2)).all()
    if len(rows) != 1:
        mismatch("ECS result reset audit missing or not unique", code)

    resource, reset_at, detail = rows[0]
    before, after = detail.get("before"), detail.get("after")
    if (
        resource != "ecs-command"
        or detail.get("command_code") != code
        or detail.get("exchange_id") != exchange_id
        or detail.get("retained_result_record_id") != result_id
        or not isinstance(before, dict)
        or before.get("applied_result_id") != semantic
        or not isinstance(after, dict)
        or "applied_result_id" not in after
        or after["applied_result_id"] is not None
        or reset_at <= fact["occurred_at"]
    ):
        mismatch("ECS result reset audit conflicts with original evidence", code)

    receipt = (
        bind.execute(
            sa.select(receipts).where(
                receipts.c.operation == "ecs.result",
                receipts.c.operation_id == semantic,
                receipts.c.active.is_(True),
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        receipt is None
        or receipt["protocol"] != "ecs-result"
        or receipt["device_code"] != root["device_code"]
        or receipt["status_code"] != 200
        or fact["http_status"] != 200
        or not fact["attempt_id"]
        or receipt["reply_attempt_id"] != fact["attempt_id"]
        or receipt["reply_wire"] != body(record(fact["response_record_id"]), "ECS result", code)
    ):
        mismatch("ECS result reset original receipt missing or conflicting", code)


def _ecs_ack(bind, records, refs, root, request, ack, record, body):
    code = root["command_code"]
    exchange_id = root["id"]
    if ack is None or ack["exchange_id"] != exchange_id:
        mismatch("ECS ACK identity", code)

    if ack["capture_origin"] == "BACKFILL":
        version = ack["source_version"]
        original = bind.scalar(
            sa.select(
                sa.exists().where(
                    refs.c.exchange_id == exchange_id,
                    refs.c.reference_kind == "LEGACY_SOURCE",
                    refs.c.namespace == "ecs_command",
                    refs.c.relation_role == "ORIGINAL_SOURCE",
                    refs.c.reference_value == code,
                    refs.c.value_digest == digest(code.encode()),
                    refs.c.source_record_id.is_(None),
                )
            )
        )
        marker = bind.scalar(
            sa.select(
                sa.exists().where(
                    records.c.exchange_id == exchange_id,
                    records.c.capture_origin == "BACKFILL",
                    records.c.record_kind == "LEGACY_SNAPSHOT",
                    records.c.source_kind == "ecs_command",
                    records.c.source_key == code,
                    records.c.source_version == version,
                )
            )
        )
        if (
            root["capture_origin"] != "BACKFILL"
            or ack["record_kind"] != "HTTP_RESPONSE_OBSERVED"
            or ack["direction"] != "INBOUND"
            or ack["attempt_id"] is not None
            or ack["body_record_id"] is not None
            or ack["http_status"] is not None
            or ack["source_kind"] != "ecs_command:ack"
            or ack["source_key"] != code
            or not valid_sha(version)
            or request["capture_origin"] != "BACKFILL"
            or request["source_kind"] != "ecs_command:request"
            or request["source_key"] != code
            or not original
            or not marker
        ):
            mismatch("ECS ACK backfill provenance", code)
        observation = ack
    else:
        lease = ack["attempt_id"]
        observation = ack
        if ack["record_kind"] == "SEND_ATTEMPT_FINISHED":
            if (
                ack["direction"] != "INTERNAL"
                or not ack["body_record_id"]
                or ack["parent_record_id"] != ack["body_record_id"]
                or ack["http_status"] not in (None, 200)
            ):
                mismatch("ECS ACK finish pointer", code)
            observation = record(ack["body_record_id"])
        if (
            not lease
            or ack["capture_origin"] != "LIVE"
            or observation is None
            or observation["exchange_id"] != exchange_id
            or observation["attempt_id"] != lease
            or observation["capture_origin"] != "LIVE"
            or observation["record_kind"] != "HTTP_RESPONSE_OBSERVED"
            or observation["direction"] != "INBOUND"
            or observation["http_status"] != 200
            or observation["network_state"] != "HTTP_RESPONSE_COMPLETE"
            or observation["protocol_verdict"] != "DELIVERED"
            or observation["body_record_id"] is not None
        ):
            mismatch("ECS ACK original HTTP observation", code)
        conclusions = bind.execute(
            sa.select(records.c.ecs_attempt_outcome, records.c.http_status).where(
                records.c.exchange_id == exchange_id,
                records.c.record_kind == "ECS_ATTEMPT_CONCLUSION",
                records.c.attempt_id == lease,
                records.c.parent_record_id == ack["id"],
            )
        ).all()
        if conclusions != [("ACK", 200)]:
            mismatch("ECS ACK conclusion missing or conflicting", code)

    wire = body(observation, "ECS ACK", code, full_only=True)
    decoded = json_object(wire)
    if (
        decoded is None
        or type(decoded.get("code")) is not int
        or (decoded["code"], decoded.get("message")) != (200, "ACK")
        or ("trace_id" in decoded and not isinstance(decoded["trace_id"], str))
    ):
        mismatch("ECS ACK body does not acknowledge the command", code)
    try:
        canonical_digest(decoded)
    except (ValueError, RecursionError):
        mismatch("ECS ACK body is not canonicalizable", code)
    return wire


def restore_active_ecs_attempts(bind, schema, metadata, messages, records):
    roots = sa.Table("integration_exchange", metadata, autoload_with=bind, resolve_fks=False)
    attempts = sa.Table(
        "reliable_delivery_attempt", metadata, autoload_with=bind, resolve_fks=False
    )
    cursor = 0
    while True:
        batch = (
            bind.execute(
                sa.select(
                    messages.c.id,
                    messages.c.lease_id,
                    messages.c.exchange_id,
                    messages.c.request_record_id,
                    messages.c.operation,
                    messages.c.operation_id,
                    messages.c.business_key,
                    messages.c.trace_id,
                    messages.c.target,
                    roots.c.command_code,
                )
                .select_from(messages.join(roots, roots.c.id == messages.c.exchange_id))
                .where(
                    messages.c.id > cursor,
                    messages.c.status == "SENDING",
                    roots.c.role == "ECS_COMMAND",
                    roots.c.status == "DISPATCHING",
                )
                .order_by(messages.c.id)
                .limit(500)
            )
            .mappings()
            .all()
        )
        if not batch:
            return
        for row in batch:
            openings = bind.execute(
                sa.select(records.c.id, records.c.occurred_at, records.c.parent_record_id)
                .where(
                    records.c.exchange_id == row["exchange_id"],
                    records.c.record_kind == "SEND_ATTEMPT_OPENED",
                    records.c.attempt_id == row["lease_id"],
                )
                .limit(2)
            ).all()
            if (
                row["lease_id"] is None
                or row["operation_id"] != row["command_code"]
                or len(openings) != 1
                or openings[0].occurred_at is None
                or (
                    openings[0].parent_record_id is not None
                    and openings[0].parent_record_id != row["request_record_id"]
                )
            ):
                raise CommandError(f"active ECS opening integrity mismatch for message {row['id']}")
            opened = openings[0]
            previous = bind.execute(
                sa.select(attempts.c.id, attempts.c.started_at, attempts.c.finished_at).where(
                    attempts.c.message_id == row["id"], attempts.c.lease_id == row["lease_id"]
                )
            ).one_or_none()
            if previous is not None:
                if previous != (opened.id, opened.occurred_at, None):
                    raise CommandError(
                        f"active ECS opening integrity mismatch for message {row['id']}"
                    )
                continue
            # The ECS adapter's method and path are fixed for the original protocol.
            bind.execute(
                attempts.insert().values(
                    message_id=row["id"],
                    lease_id=row["lease_id"],
                    id=opened.id,
                    protocol="ecs-command",
                    exchange_id=row["exchange_id"],
                    request_record_id=row["request_record_id"],
                    operation=row["operation"],
                    operation_id=row["operation_id"],
                    business_key=row["business_key"],
                    trace_id=row["trace_id"],
                    peer_endpoint=row["target"],
                    traffic_source="PEER",
                    http_method="POST",
                    endpoint_path="/api/v1/device/command",
                    started_at=opened.occurred_at,
                    attempt_state="OPEN",
                    late=False,
                    revision=1,
                )
            )
        cursor = batch[-1]["id"]


def _restore_static_openings(bind, messages, roots, records, attempts):
    # Old static routes may lack the exact endpoint path. Keep the proof for
    # recovery and replacement, without synthesizing a historic HTTP row.
    observations = records.alias("lease_observation")
    observed = sa.exists().where(
        observations.c.exchange_id == messages.c.exchange_id,
        observations.c.attempt_id == messages.c.lease_id,
        observations.c.record_kind.in_(("HTTP_RESPONSE_OBSERVED", "SEND_ATTEMPT_FINISHED")),
    )
    rows = bind.execute(
        sa.select(
            messages.c.id,
            messages.c.lease_id,
            messages.c.exchange_id,
            messages.c.request_record_id,
            messages.c.operation,
            messages.c.operation_id,
            messages.c.business_key,
            messages.c.trace_id,
            messages.c.target,
            messages.c.frozen_request_wire,
            messages.c.lease_not_started,
            roots.c.protocol,
            records.c.id.label("opening_id"),
            records.c.occurred_at,
            records.c.parent_record_id,
            attempts.c.id.label("runtime_id"),
            attempts.c.exchange_id.label("runtime_exchange"),
            attempts.c.request_record_id.label("runtime_request"),
            attempts.c.started_at.label("runtime_started"),
            attempts.c.finished_at.label("runtime_finished"),
            observed.label("observed"),
        )
        .select_from(
            messages.join(roots, messages.c.exchange_id == roots.c.id)
            .outerjoin(
                records,
                sa.and_(
                    records.c.exchange_id == roots.c.id,
                    records.c.attempt_id == messages.c.lease_id,
                    records.c.record_kind == "SEND_ATTEMPT_OPENED",
                ),
            )
            .outerjoin(
                attempts,
                sa.and_(
                    attempts.c.message_id == messages.c.id,
                    attempts.c.lease_id == messages.c.lease_id,
                ),
            )
        )
        .where(
            messages.c.status == "SENDING",
            messages.c.delivery_snapshot.is_(None),
        )
    ).mappings()
    seen = set()
    for row in rows:
        if row["id"] in seen:
            mismatch("static opening missing or ambiguous", row["id"])
        seen.add(row["id"])
        if row["opening_id"] is None:
            # Only a marker for this lease can prove the sender never crossed its gate.
            if row["lease_not_started"] is not True or row["runtime_id"] or row["observed"]:
                mismatch("static opening missing or ambiguous", row["id"])
            continue
        if (
            row["lease_not_started"] is True
            or row["occurred_at"] is None
            or row["parent_record_id"] is not None
            and row["parent_record_id"] != row["request_record_id"]
        ):
            mismatch("static opening identity conflict", row["id"])
        if row["runtime_id"] is not None:
            if (
                row["runtime_id"] != row["opening_id"]
                or row["runtime_exchange"] != row["exchange_id"]
                or row["runtime_request"] != row["request_record_id"]
                or row["runtime_started"] != row["occurred_at"]
                or row["runtime_finished"] is not None
            ):
                mismatch("static opening identity conflict", row["id"])
            continue
        bind.execute(
            attempts.insert().values(
                id=row["opening_id"],
                message_id=row["id"],
                lease_id=row["lease_id"],
                exchange_id=row["exchange_id"],
                request_record_id=row["request_record_id"],
                operation=row["operation"],
                operation_id=row["operation_id"],
                protocol=row["protocol"],
                business_key=row["business_key"],
                trace_id=row["trace_id"],
                peer_endpoint=row["target"],
                request_wire=row["frozen_request_wire"],
                started_at=row["occurred_at"],
                traffic_source="PEER",
                http_method="POST",
                endpoint_path="",
                attempt_state="OPEN",
                late=False,
                revision=1,
            )
        )


def _transport(bind, tasks, records, refs, receipts, record, body):
    def receipt_fact(fact, task_id, operations, revision=None):
        if fact is None or fact["record_kind"] != "CALLBACK_RECEIVED":
            mismatch("Transport callback fact missing", task_id)
        receipt = (
            bind.execute(
                sa.select(receipts).where(
                    receipts.c.id == fact["exchange_id"], receipts.c.operation.in_(operations)
                )
            )
            .mappings()
            .one_or_none()
        )
        wire = body(fact, "Transport callback", task_id, required=False)
        if receipt is None:
            mismatch("Transport callback receipt missing", task_id)
        if wire is not None:
            try:
                envelope = json.loads(wire)
                data = envelope["data"]
                matched = (
                    envelope["operation_id"] == receipt["operation_id"]
                    and envelope["operation"] == receipt["operation"]
                    and data["transport_task_id"] == task_id
                    and (revision is None or data["outcome_revision"] == revision)
                )
            except (ValueError, KeyError, TypeError, RecursionError):
                matched = False
            if not matched:
                mismatch("Transport callback identity conflict", task_id)
        return receipt

    def source(task_id, revision):
        links = (
            bind.execute(
                sa.select(refs).where(
                    refs.c.reference_kind == "TRANSPORT_TASK",
                    refs.c.namespace == "transport-outcome",
                    refs.c.reference_value == f"{task_id}:{revision}",
                    refs.c.relation_role == "RESULT_SOURCE",
                )
            )
            .mappings()
            .all()
        )
        if len(links) > 1:
            mismatch("Transport result source not unique", task_id)
        if not links:
            return None
        link = links[0]
        fact = record(link["source_record_id"])
        if fact is None or fact["exchange_id"] != link["exchange_id"]:
            mismatch("Transport result source missing", task_id)
        receipt = receipt_fact(fact, task_id, ("transport.task.resulted@v1",), revision)
        return receipt["receipt_id"], fact["id"], receipt["id"]

    def legacy_pending(task, snapshot):
        # An exact snapshot from the earlier, stopped-writer backfill establishes
        # that the pending outcome already existed before live source links.
        fields = {name: task[name] for name in SOURCES["transport_task"].split()}
        versions = [snapshot_version(fields)]
        if snapshot is not None:
            # The earlier contract moved the raw acceptance fields out of the task.
            original = {key: value for key, value in snapshot.items() if key != "format_version"}
            versions.append(snapshot_version(fields | {"submission_acceptance": original}))
        return bind.scalar(
            sa.select(
                sa.exists().where(
                    records.c.exchange_id == task["submission_exchange_id"],
                    records.c.capture_origin == "BACKFILL",
                    records.c.record_kind == "LEGACY_SNAPSHOT",
                    records.c.source_kind == "transport_task",
                    records.c.source_key == str(task["id"]),
                    records.c.source_version.in_(versions),
                )
            )
        )

    for task in bind.execute(sa.select(tasks)).mappings():
        task_id = task["transport_task_id"]
        pending = task["pending_outcome"]
        revision = task["applied_outcome_revision"]
        values = {}
        evidence = task["submission_acceptance"]
        snapshot = None
        if evidence is not None:
            request = body(
                record(task["acceptance_request_record_id"]),
                "Transport acceptance request",
                task_id,
            )
            response = body(
                record(task["acceptance_response_record_id"]),
                "Transport acceptance response",
                task_id,
            )
            snapshot = {
                **evidence,
                "request_raw": request.decode(),
                "response_raw": response.decode(),
            }
            proof = {
                k: v for k, v in snapshot.items() if k not in {"format_version", "evidence_sha256"}
            }
            hashed = digest(
                json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                    "ascii"
                )
            )
            if (
                snapshot.get("request_sha256") != digest(request)
                or snapshot.get("evidence_sha256") != hashed
            ):
                mismatch("Transport acceptance snapshot conflict", task_id)
            values["submission_acceptance"] = snapshot
        if pending is not None:
            try:
                pending_revision = pending["revision"]
                data = pending["data"]
                if (
                    type(pending_revision) is not int
                    or pending_revision < 1
                    or data["outcome_revision"] != pending_revision
                    or data["transport_task_id"] != task_id
                    or data["kind"] != task["kind"]
                    or (task["outcome_digests"] or {}).get(str(pending_revision))
                    != canonical_digest(data)
                ):
                    mismatch("Transport pending result digest conflict", task_id)
            except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
                mismatch("Transport pending result digest conflict", task_id)
            identity = source(task_id, pending["revision"])
            if identity is None and (
                task["pending_result_source_unknown"] is False or not legacy_pending(task, snapshot)
            ):
                mismatch("Transport pending result source missing", task_id)
            if identity is not None:
                fact = record(identity[1])
                original = (
                    bind.execute(sa.select(receipts).where(receipts.c.receipt_id == identity[0]))
                    .mappings()
                    .one()
                )
                request = body(fact, "Transport pending result source", task_id, required=False)
                if request is not None:
                    envelope = json.loads(request)
                    if original["compare_mode"] != "EFFECTIVE" or original[
                        "effective_digest"
                    ] != canonical_digest(
                        dict(
                            operation=envelope["operation"],
                            operation_id=envelope["operation_id"],
                            timestamp=envelope["timestamp"],
                            data=data,
                        )
                    ):
                        mismatch("Transport pending result source payload conflict", task_id)
                else:
                    mismatch("Transport pending result source body missing", task_id)
            values["pending_result_receipt_id"] = identity[0] if identity else None
            values["pending_result_source_unknown"] = identity is None
        if revision is not None:
            identity = source(task_id, revision)
            applied = (
                bind.execute(
                    sa.select(records).where(
                        records.c.exchange_id == task["submission_exchange_id"],
                        records.c.record_kind == "RESULT_APPLIED",
                        records.c.outcome_revision == revision,
                    )
                )
                .mappings()
                .all()
            )
            if len(applied) > 1:
                mismatch("Transport applied evidence not unique", task_id)
            if not applied:
                mismatch("Transport applied evidence missing", task_id)
            if applied:
                applied = applied[0]
                details = applied["metadata"] or {}
                parent_id = applied["parent_record_id"]
                if (
                    parent_id != (identity[1] if identity else None)
                    or identity is None
                    and details.get("coverage_reason")
                    not in ("LEGACY_ORIGINAL_CALLBACK_UNKNOWN", "LEGACY_SOURCE_UNAVAILABLE")
                ):
                    mismatch("Transport original result source conflict", task_id)
                if applied["effective_payload"] != task["applied_outcome"]:
                    mismatch("Transport applied result conflict", task_id)
                trigger = details.get("trigger_record_id")
                if (
                    not trigger
                    and details.get("coverage_reason") != "LEGACY_ORIGINAL_CALLBACK_UNKNOWN"
                ):
                    mismatch("Transport trigger identity", task_id)
                edges = bind.execute(
                    sa.select(refs.c.relation_role, refs.c.related_exchange_id).where(
                        refs.c.exchange_id == task["submission_exchange_id"],
                        refs.c.source_record_id == applied["id"],
                        refs.c.relation_role.in_(("RESULT_SOURCE", "TRIGGER")),
                    )
                ).all()
                expected = []
                if identity:
                    expected.append(("RESULT_SOURCE", identity[2]))
                if trigger:
                    try:
                        fact = record(UUID(trigger))
                    except (ValueError, TypeError, AttributeError):
                        mismatch("Transport trigger identity", task_id)
                    receipt = receipt_fact(
                        fact,
                        task_id,
                        ("transport.task.resulted@v1", "transport.task.member_position_changed@v1"),
                    )
                    values["applied_trigger_receipt_id"] = receipt["receipt_id"]
                    expected.append(("TRIGGER", receipt["id"]))
                if sorted(edges) != sorted(expected):
                    mismatch("Transport applied source or trigger reference conflict", task_id)
            values["applied_result_receipt_id"] = identity[0] if identity else None
        if values:
            bind.execute(tasks.update().where(tasks.c.id == task["id"]).values(**values))


def _retention(bind, roots, refs, holds, messages, tasks):
    links = bind.execute(
        sa.select(
            refs.c.reference_kind,
            refs.c.namespace,
            refs.c.reference_value,
            roots.c.receipt_id,
            roots.c.command_code,
            roots.c.id,
        )
        .select_from(refs.join(roots, roots.c.id == refs.c.exchange_id))
        .where(
            refs.c.reference_kind.like("BUSINESS_%"),
            roots.c.role != "DIAGNOSTIC",
        )
    ).all()
    values = set()
    for kind, owner, value, receipt, code, root_id in links:
        if not owner or not value:
            mismatch("retention owner identity missing", root_id)
        if receipt is not None:
            subjects = [("RECEIPT", receipt)]
        elif code is not None:
            subjects = [("ECS_COMMAND", code)]
        else:
            subjects = [
                ("WMS_MESSAGE", identity)
                for identity in bind.scalars(
                    sa.select(messages.c.id).where(messages.c.exchange_id == root_id)
                )
            ] + [
                ("TRANSPORT_TASK", identity)
                for identity in bind.scalars(
                    sa.select(tasks.c.transport_task_id).where(
                        tasks.c.submission_exchange_id == root_id
                    )
                )
            ]
            if not subjects:
                historic = bind.scalars(
                    sa.select(refs.c.reference_value).where(
                        refs.c.exchange_id == root_id,
                        refs.c.reference_kind == "RELIABLE_MESSAGE",
                        refs.c.namespace == "id",
                        refs.c.relation_role == "ORIGINAL_MESSAGE",
                    )
                ).all()
                if len(historic) != 1 or not historic[0].isascii() or not historic[0].isdecimal():
                    mismatch("retention source missing or ambiguous", root_id)
                subjects = [("WMS_MESSAGE", historic[0])]
        values.update(
            (subject, str(identity), kind, owner, value) for subject, identity in subjects
        )
    if values:
        bind.execute(
            holds.insert(),
            [
                dict(
                    zip(
                        ("subject_kind", "subject_id", "owner_kind", "owner_domain", "owner_id"),
                        row,
                        strict=True,
                    )
                )
                for row in sorted(values)
            ],
        )
