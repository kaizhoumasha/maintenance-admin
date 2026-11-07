"""Frozen, body-free SQL projections shipped with integrationhistoryexpand."""

from sqlalchemy import text

VIEWS = ("integration_exchange_summary", "integration_record_summary")


def drop_views(bind):
    prefix = "enterprise_backend." if bind.dialect.name == "postgresql" else ""
    for name in VIEWS:
        bind.execute(text(f"DROP VIEW IF EXISTS {prefix}{name}"))


def create_views(bind):
    p = "enterprise_backend." if bind.dialect.name == "postgresql" else ""
    # Ambiguous runtime associations must not multiply rows or select an arbitrary authority.
    bind.execute(
        text(f"""
        CREATE VIEW {p}integration_exchange_summary AS
        SELECT e.id,e.schema_version,e.role,e.peer_system,e.protocol,e.direction,
               e.interaction_kind,e.operation,e.operation_id,e.message_type,
               e.identity_kind,e.identity_value,e.owner_domain,e.device_code,
               e.created_at,e.recorded_at,e.capture_origin,e.coverage,
               COALESCE(e.delivery_status,m.status,'UNKNOWN') AS delivery_state,
               m.delivery_uncertain,m.last_verdict AS protocol_verdict,
               CASE WHEN e.current_response_record_id IS NOT NULL THEN e.status_code
                    ELSE m.last_http_status END AS protocol_http_status,
               CASE WHEN e.current_response_record_id IS NOT NULL THEN 'PROTOCOL_PREPARED'
                    ELSE 'PEER_OBSERVATION' END AS response_scope,
               COALESCE(e.current_response_record_id,m.last_response_record_id,
                        m.terminal_response_record_id,e.ack_record_id) AS protocol_source_record_id,
               reply.protocol_code,
               CASE WHEN (e.status IN ('SUCCEEDED','FAILED') OR
                          t.applied_outcome_revision IS NOT NULL)
                    AND NOT EXISTS (
                        SELECT 1 FROM {p}integration_record f WHERE f.exchange_id=e.id
                        AND f.evidence_class='CORE' AND (
                            (f.record_kind='RESULT_EVIDENCE' AND f.applied=TRUE
                             AND f.semantic_digest=e.applied_result_id) OR
                            (e.applied_result_id IS NULL AND f.record_kind='RESULT_APPLIED'
                             AND f.outcome_revision=t.applied_outcome_revision)))
                    THEN 'UNKNOWN'
                    ELSE COALESCE(e.status,t.outcome_status,'UNKNOWN') END AS application_state,
               t.applied_outcome_revision AS outcome_revision,e.needs_reconciliation,
               (SELECT f.id FROM {p}integration_record f WHERE f.exchange_id=e.id
                AND f.evidence_class='CORE' AND (
                    (f.record_kind='RESULT_EVIDENCE' AND f.applied=TRUE
                     AND f.semantic_digest=e.applied_result_id) OR
                    (e.applied_result_id IS NULL AND f.record_kind='RESULT_APPLIED'
                     AND f.outcome_revision=t.applied_outcome_revision))
                ORDER BY f.id LIMIT 1) AS application_source_record_id
        FROM {p}integration_exchange e
        LEFT JOIN {p}reliable_message m ON m.exchange_id=e.id AND NOT EXISTS (
            SELECT 1 FROM {p}reliable_message other
            WHERE other.exchange_id=e.id AND other.id<>m.id)
        LEFT JOIN {p}transport_task t ON t.submission_exchange_id=e.id AND NOT EXISTS (
            SELECT 1 FROM {p}transport_task other
            WHERE other.submission_exchange_id=e.id AND other.id<>t.id)
        LEFT JOIN {p}integration_record reply ON reply.id=e.current_response_record_id
    """)
    )
    bind.execute(
        text(f"""
        CREATE VIEW {p}integration_record_summary AS
        SELECT r.id,r.exchange_id,r.record_kind,r.evidence_class,r.attempt_id,r.parent_record_id,
               r.direction,r.occurred_at,r.recorded_at,r.capture_origin,r.http_status,r.network_state,
               r.protocol_verdict,r.protocol_code,r.business_result,r.application_state,r.late,
               r.outcome_revision,
               CASE WHEN r.body_record_id IS NOT NULL THEN b.body_state
                    ELSE r.body_state END AS body_state,
               CASE WHEN r.body_record_id IS NOT NULL THEN b.body_sha256
                    ELSE r.body_sha256 END AS body_sha256,
               CASE WHEN r.body_record_id IS NOT NULL THEN b.stored_bytes
                    ELSE r.stored_bytes END AS stored_bytes,
               r.body_record_id,r.source_kind,r.source_key,r.source_version,r.response_record_id
        FROM {p}integration_record r
        LEFT JOIN {p}integration_record b ON b.id=r.body_record_id
    """)
    )
