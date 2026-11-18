"""Recover terminal Transport submissions with complete historical no-send evidence."""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op

from app.integrations.runtime_authority import LEGACY_TABLES, check_reconciled
from app.strict_json import json_object

revision = "scoped_unsent_transport_v1"
down_revision = "scoped_dispatch_evidence_v1"
branch_labels = None
depends_on = None


def _legacy_proof(prefix, attempts):
    records = f"{prefix}integration_record"
    return f"""
        EXISTS (
            SELECT 1 FROM {prefix}integration_exchange e
            JOIN {records} r ON r.id = e.request_record_id AND r.exchange_id = e.id
            WHERE e.id = m.exchange_id AND e.operation_id = m.operation_id
              AND e.operation = m.operation AND e.protocol = m.protocol AND e.target_key = m.target
              AND e.owner_domain = m.scope_owner AND e.role = 'OUTGOING'
              AND e.ack_record_id IS NULL AND e.current_response_record_id IS NULL
              AND r.id = m.request_record_id AND r.record_kind = 'REQUEST_REGISTERED'
              AND r.body_state = 'FULL' AND r.raw_body = m.frozen_request_wire
              AND r.body_sha256 = m.frozen_request_sha256
        )
        AND NOT EXISTS (
            SELECT 1 FROM {records} r WHERE r.exchange_id = m.exchange_id
              AND r.record_kind IN ('HTTP_RESPONSE_OBSERVED', 'RESULT_EVIDENCE', 'RESULT_APPLIED')
        )
        AND m.attempts = (
            SELECT COUNT(*) FROM {records} r WHERE r.exchange_id = m.exchange_id
              AND r.record_kind = 'SEND_ATTEMPT_OPENED'
        )
        AND m.attempts = (
            SELECT COUNT(*) FROM {records} r WHERE r.exchange_id = m.exchange_id
              AND r.record_kind = 'SEND_ATTEMPT_FINISHED'
        )
        AND (m.last_response_record_id IS NULL OR EXISTS (
            SELECT 1 FROM {records} r
            WHERE r.id = m.last_response_record_id AND r.exchange_id = m.exchange_id
              AND r.record_kind = 'SEND_ATTEMPT_FINISHED'
        ))
        AND m.attempts = (
            SELECT COUNT(*) FROM {attempts} a JOIN {records} o ON o.id = a.id
            WHERE a.message_id = m.id
              AND (a.peer_endpoint = m.target OR a.peer_endpoint = o.metadata ->> 'origin')
              AND o.exchange_id = a.exchange_id AND o.attempt_id = a.lease_id
              AND o.record_kind = 'SEND_ATTEMPT_OPENED' AND o.capture_origin = 'LIVE'
              AND o.direction = 'OUTBOUND' AND o.occurred_at = a.started_at
              AND o.event_key = 'OPEN:' || a.lease_id AND o.parent_record_id = a.request_record_id
              AND EXISTS (
                  SELECT 1 FROM {records} f
                  WHERE f.exchange_id = a.exchange_id AND f.attempt_id = a.lease_id
                    AND f.record_kind = 'SEND_ATTEMPT_FINISHED' AND f.capture_origin = 'LIVE'
                    AND f.direction = 'INTERNAL' AND NOT f.late AND f.occurred_at = a.finished_at
                    AND f.network_state = 'NOT_SENT'
                    AND f.application_state IN ('PENDING', 'STALLED')
                    AND f.parent_record_id IS NULL AND f.body_record_id IS NULL
                    AND f.response_record_id IS NULL AND f.raw_body IS NULL
                    AND f.http_status IS NULL AND f.protocol_verdict IS NULL
              )
        )
    """


def _runtime_proof(prefix, attempts, has_legacy):
    if has_legacy:
        # A runtime-native registration has no identity in the fenced legacy journal.
        origin = f"""
            NOT EXISTS (
                SELECT 1 FROM {prefix}integration_exchange e
                WHERE e.id = m.exchange_id OR e.request_record_id = m.request_record_id
                  OR (e.operation = m.operation AND e.operation_id = m.operation_id)
            )
            AND NOT EXISTS (
                SELECT 1 FROM {prefix}integration_record r
                WHERE r.exchange_id = m.exchange_id OR r.id = m.request_record_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM {attempts} a JOIN {prefix}integration_record r ON r.id = a.id
                WHERE a.message_id = m.id
            )
            AND NOT EXISTS (
                SELECT 1 FROM {prefix}integration_reference r
                WHERE r.reference_kind = 'TRANSPORT_TASK' AND r.reference_value = m.business_key
            )
        """
    else:
        # The only finished attempt used the final lease. Legacy finalization always left a pointer.
        origin = f"""
            m.attempts = 1 AND m.lease_not_started IS FALSE
            AND EXISTS (
                SELECT 1 FROM {attempts} a WHERE a.message_id = m.id
                  AND a.started_at >= m.last_attempt_at
                  AND a.failure_kind IS NOT DISTINCT FROM m.last_failure_kind
            )
        """
    return f"""
        m.last_response_record_id IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM {attempts} a WHERE a.message_id = m.id AND a.peer_endpoint <> m.target
        )
        AND ({origin})
    """


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    prefix = f"{schema}." if schema else ""
    inspector = sa.inspect(bind)
    legacy_tables = [table for table in LEGACY_TABLES if inspector.has_table(table, schema=schema)]
    has_legacy = tuple(legacy_tables) == LEGACY_TABLES
    lock_tables = (
        "reliable_message",
        "reliable_delivery_attempt",
        "transport_task",
        "transport_http_exchange",
        *legacy_tables,
    )
    if schema:
        bind.exec_driver_sql(
            "LOCK TABLE "
            + ", ".join(prefix + table for table in lock_tables)
            + " IN SHARE ROW EXCLUSIVE MODE NOWAIT"
        )
    messages = f"{prefix}reliable_message"
    attempts = f"{prefix}reliable_delivery_attempt"
    legacy = _legacy_proof(prefix, attempts) if has_legacy else "FALSE"
    try:
        check_reconciled(bind, use_runtime=True, query_revision=down_revision)
    except RuntimeError:
        runtime = "FALSE"
    else:
        runtime = _runtime_proof(prefix, attempts, has_legacy)

    candidates = bind.exec_driver_sql(f"""
        SELECT m.id, m.operation, m.operation_id, m.business_key,
               m.frozen_request_wire, m.frozen_request_sha256
        FROM {messages} m
        JOIN {prefix}transport_task t ON t.transport_task_id = m.business_key
            AND t.operation_id = m.operation_id AND t.owner_domain = m.scope_owner
            AND t.submission_exchange_id = m.exchange_id
        WHERE m.protocol = 'wms-transport' AND m.operation = 'transport.task.submit@v1'
          AND m.scope_owner IS NOT NULL AND m.delivery_snapshot IS NULL
          AND m.status IN ('ABANDONED', 'EXPIRED', 'FAILED')
          AND m.dispatch_started_at IS NOT NULL AND m.dispatch_attempt_count IS NULL
          AND m.lease_id IS NULL AND m.lease_until IS NULL AND NOT m.delivery_uncertain
          AND m.attempts > 0 AND m.not_received_count = m.attempts
          AND NOT m.replacement_blocked AND m.superseded_by_message_id IS NULL
          AND m.frozen_request_wire IS NOT NULL
          AND m.terminal_response_record_id IS NULL AND m.terminal_response_wire IS NULL
          AND m.terminal_response_at IS NULL AND m.last_response_wire IS NULL
          AND m.last_verdict IS NULL AND m.last_http_status IS NULL
          AND t.submission_acceptance IS NULL AND t.acceptance_request_record_id IS NULL
          AND t.acceptance_response_record_id IS NULL AND t.pending_outcome IS NULL
          AND t.applied_outcome IS NULL AND t.outcome_status IS NULL
          AND t.applied_outcome_revision IS NULL AND t.outcome_digests IS NULL
          AND t.position_observation IS NULL AND t.member_positions IS NULL
          AND t.pending_result_receipt_id IS NULL AND t.applied_result_receipt_id IS NULL
          AND t.applied_trigger_receipt_id IS NULL
          AND NOT COALESCE(t.pending_result_source_unknown, FALSE)
          AND NOT EXISTS (
              SELECT 1 FROM {prefix}transport_http_exchange h
              WHERE (h.transport_task_id = m.business_key
                OR (h.message_id = m.id AND h.lease_id IS NOT NULL))
                AND (h.interaction_kind <> 'SUBMIT' OR h.attempt_state NOT IN ('OPEN', 'NOT_SENT')
                  OR h.response_wire_state <> 'UNAVAILABLE' OR h.response_wire IS NOT NULL
                  OR h.response_wire_sha256 IS NOT NULL OR h.response_wire_bytes IS NOT NULL
                  OR h.response_payload IS NOT NULL OR h.protocol_code IS NOT NULL
                  OR h.response_headers IS NOT NULL OR h.http_status IS NOT NULL
                  OR h.response_observed_at IS NOT NULL OR h.late_response_at IS NOT NULL
                  OR h.protocol_verdict IS NOT NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM {attempts} a WHERE a.id = h.id AND a.message_id = m.id
                        AND h.message_id = m.id AND a.lease_id = h.lease_id
                        AND h.transport_task_id = m.business_key
                  ))
          )
          AND NOT EXISTS (
              SELECT 1 FROM {messages} other WHERE other.id <> m.id
                AND (other.exchange_id = m.exchange_id
                  OR other.request_record_id = m.request_record_id
                  OR (other.operation = m.operation AND other.operation_id = m.operation_id)
                  OR (other.operation = m.operation AND other.business_key = m.business_key))
          )
          AND NOT EXISTS (
              SELECT 1 FROM {prefix}transport_task other
              WHERE other.id <> t.id AND other.submission_exchange_id = m.exchange_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM {attempts} other WHERE other.message_id <> m.id
                AND (other.exchange_id = m.exchange_id
                  OR other.request_record_id = m.request_record_id
                  OR (other.operation = m.operation AND other.operation_id = m.operation_id)
                  OR (other.operation = m.operation AND other.business_key = m.business_key))
          )
          AND (
              SELECT COUNT(*) = m.attempts AND MIN(a.started_at) = m.dispatch_started_at
              FROM {attempts} a WHERE a.message_id = m.id
          )
          AND m.attempts = (
              SELECT COUNT(*) FROM {attempts} a
              WHERE a.message_id = m.id AND a.exchange_id = m.exchange_id
                AND a.request_record_id = m.request_record_id
                AND a.request_wire = m.frozen_request_wire
                AND a.protocol = m.protocol AND a.operation = m.operation
                AND a.operation_id = m.operation_id AND a.business_key = m.business_key
                AND a.started_at >= m.created_at AND a.finished_at >= a.started_at
                AND a.finished_at <= m.updated_at AND a.close_reason = 'FINISHED'
                AND a.attempt_state = 'NOT_SENT' AND NOT a.late
                AND a.response_record_id IS NULL AND a.response_state IS NULL
                AND a.response_wire IS NULL AND a.response_http_status IS NULL
                AND a.response_verdict IS NULL AND a.response_observed_at IS NULL
                AND a.response_completed_at IS NULL AND a.response_headers IS NULL
          )
          AND (({legacy}) OR ({runtime}))
    """)
    repair = sa.text(f"""
        UPDATE {messages}
        SET dispatch_started_at = NULL, dispatch_attempt_count = attempts
        WHERE id = :id
    """)
    for batch in candidates.mappings().partitions(100):
        verified = []
        for row in batch:
            wire = row["frozen_request_wire"]
            if sha256(wire).hexdigest() != row["frozen_request_sha256"]:
                continue
            request = json_object(wire) or {}
            data = request.get("data")
            if (
                request.get("operation") == row["operation"]
                and request.get("operation_id") == row["operation_id"]
                and isinstance(data, dict)
                and data.get("transport_task_id") == row["business_key"]
            ):
                verified.append({"id": row["id"]})
        if verified:
            bind.execute(repair, verified)


def downgrade():
    # Preserve the verified delivery fact across schema downgrades.
    pass
