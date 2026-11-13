"""Body-free compatibility projections over HTTP attempts and original runtime IDs."""

from sqlalchemy import text


def create_http_views(bind):
    pg = bind.dialect.name == "postgresql"
    p = "enterprise_backend." if pg else ""
    uid = "uuid" if pg else "text"
    # SQLite stores ORM UUIDs as 32 hex digits.
    operation_id = (
        "CAST(h.operation_id AS text)"
        if pg
        else "substr(h.operation_id,1,8)||'-'||substr(h.operation_id,9,4)||'-'||"
        "substr(h.operation_id,13,4)||'-'||substr(h.operation_id,17,4)||'-'||"
        "substr(h.operation_id,21,12)"
    )
    common = (
        "id",
        "direction",
        "interaction_kind",
        "started_at",
        "finished_at",
        "response_observed_at",
        "attempt_state",
        "trace_id",
        "reason_code",
        "failure_kind",
        "close_reason",
        "http_status",
        "protocol_code",
        "protocol_verdict",
        "reported_result",
        "handling_outcome",
        "late_response_at",
        "message_id",
        "lease_id",
        "receipt_id",
        "replayed_from_id",
        "request_wire_state",
        "response_wire_state",
        "request_wire_sha256",
        "response_wire_sha256",
        "request_wire_bytes",
        "response_wire_bytes",
    )
    base = ", ".join("h." + name for name in common)
    rows = f"""
        SELECT {base}, 'WMS' AS peer, {operation_id} AS wms_id, h.operation AS operation_name,
            NULL AS command_code, NULL AS device_code, NULL AS message_type,
            NULL AS transport_task_id, NULL AS task_match_state,
            CAST(NULL AS bigint) AS outcome_revision
        FROM {p}wms_http_exchange h
        UNION ALL SELECT {base}, 'ECS', NULL,
            CASE WHEN h.interaction_kind='COMMAND' THEN 'ecs.command'
                 WHEN h.interaction_kind='EVENT' THEN 'ecs.event'
                 WHEN h.interaction_kind='RESULT' THEN 'ecs.result' ELSE 'ecs.status' END,
            h.command_code, h.device_code, COALESCE(h.task_type,h.event_type), NULL, NULL, NULL
        FROM {p}ecs_http_exchange h
        UNION ALL SELECT {base}, 'TRANSPORT', NULL,
            CASE WHEN h.interaction_kind='SUBMIT' THEN 'transport.task.submit@v1'
                 WHEN h.interaction_kind='RESULT_CALLBACK' THEN 'transport.task.resulted@v1'
                 ELSE 'transport.task.member_position_changed@v1' END,
            NULL, NULL, NULL, h.transport_task_id, h.task_match_state, h.outcome_revision
        FROM {p}transport_http_exchange h
    """

    def side_id(digit):
        # Exchange IDs are v7. The compatibility IDs retain every identity bit
        # and encode the side in the version nibble for indexed reverse lookup.
        raw = "replace(CAST(h.id AS text),'-','')"
        return f"CAST((substr({raw},1,12)||'{digit}'||substr({raw},14)) AS {uid})"

    bind.execute(
        text(f"""
        CREATE VIEW {p}integration_exchange_summary AS
        SELECT h.id, 2 AS schema_version,
            CASE WHEN h.receipt_id IS NOT NULL THEN 'RECEIPT'
                 WHEN h.interaction_kind='COMMAND' THEN 'ECS_COMMAND'
                 WHEN h.message_id IS NOT NULL THEN 'OUTGOING' ELSE 'DIAGNOSTIC' END AS role,
            h.peer AS peer_system,
            CASE h.interaction_kind
                WHEN 'DECISION' THEN 'wms-decisions' WHEN 'FACT' THEN 'wms-facts'
                WHEN 'CALLBACK' THEN 'wms-callback' WHEN 'COMMAND' THEN 'ecs-command'
                WHEN 'EVENT' THEN 'ecs-event' WHEN 'RESULT' THEN 'ecs-result'
                WHEN 'STATUS' THEN 'ecs-status' ELSE 'wms-transport' END AS protocol,
            h.direction, h.interaction_kind, h.operation_name AS operation,
            COALESCE(h.wms_id,r.operation_id) AS operation_id, h.message_type,
            CASE WHEN h.peer='TRANSPORT' AND h.transport_task_id IS NOT NULL
                    THEN 'TRANSPORT_TASK_ID'
                 WHEN h.command_code IS NOT NULL THEN 'COMMAND_CODE'
                 WHEN h.interaction_kind='EVENT' AND r.receipt_id IS NOT NULL
                    THEN 'EVENT_DIGEST'
                 WHEN h.wms_id IS NOT NULL THEN 'OPERATION_ID'
                 WHEN h.device_code IS NOT NULL THEN 'DEVICE_CODE' ELSE 'REQUEST_ID'
            END AS identity_kind,
            COALESCE(h.transport_task_id,h.command_code,h.wms_id,
                CASE WHEN h.interaction_kind='EVENT' THEN r.operation_id END,
                h.device_code,CAST(h.id AS text)) AS identity_value,
            COALESCE(c.owner_domain,t.owner_domain,r.owner_domain,m.scope_owner) AS owner_domain,
            h.device_code, h.started_at AS created_at, h.started_at AS recorded_at,
            'LIVE' AS capture_origin,
            CASE WHEN h.request_wire_state='FULL' AND h.response_wire_state='FULL'
                 THEN 'DECLARED' ELSE 'PARTIAL' END AS coverage,
            COALESCE(m.status,c.delivery_status,'UNKNOWN') AS delivery_state,
            m.delivery_uncertain, h.protocol_verdict, h.http_status AS protocol_http_status,
            CASE WHEN h.http_status IS NULL THEN NULL
                 WHEN h.direction='INBOUND' THEN 'PROTOCOL_PREPARED' ELSE 'PEER_OBSERVATION' END
                AS response_scope,
            CASE WHEN h.http_status IS NOT NULL THEN {side_id("5")} END
                AS protocol_source_record_id,
            h.protocol_code,
            CASE WHEN h.interaction_kind='EVENT' AND r.application_ref IS NOT NULL
                 THEN 'RECORDED_APPLICATION'
                 ELSE COALESCE(c.status,t.outcome_status,'UNKNOWN') END AS application_state,
            t.applied_outcome_revision AS outcome_revision, c.needs_reconciliation,
            CAST(NULL AS {uid}) AS application_source_record_id,
            h.trace_id,h.reason_code,h.failure_kind,h.close_reason,h.late_response_at,
            h.started_at,h.finished_at,h.response_observed_at,h.attempt_state,
            h.request_wire_state,h.response_wire_state,h.request_wire_sha256,h.response_wire_sha256,
            h.request_wire_bytes,h.response_wire_bytes,h.reported_result,h.handling_outcome,
            h.message_id,h.lease_id,h.receipt_id,r.active AS receipt_active,h.command_code,
            h.transport_task_id,h.task_match_state,h.outcome_revision AS reported_revision,
            c.command_code IS NOT NULL AS command_matched,
            c.ack_record_id IS NOT NULL AS ack_saved,h.replayed_from_id,
            t.pending_result_receipt_id,t.applied_result_receipt_id,t.applied_trigger_receipt_id,
            c.applied_result_id AS applied_result_digest,
            {side_id("4")} AS request_record_id,
            CASE WHEN h.http_status IS NOT NULL THEN {side_id("5")} END AS response_record_id,
            m.superseded_by_message_id
        FROM ({rows}) h
        LEFT JOIN {p}reliable_message m ON m.id=h.message_id
        LEFT JOIN {p}callback_receipt r ON r.receipt_id=h.receipt_id
        LEFT JOIN {p}ecs_command c ON c.command_code=h.command_code AND c.device_code=h.device_code
        LEFT JOIN {p}transport_task t ON t.transport_task_id=h.transport_task_id
            AND h.task_match_state='MATCHED'
    """)
    )
    bind.execute(
        text(f"""
        CREATE VIEW {p}integration_record_summary AS
        SELECT {side_id("4")} AS id,h.id AS exchange_id,
            CASE WHEN h.direction='INBOUND' THEN 'CALLBACK_RECEIVED' ELSE 'REQUEST_REGISTERED' END
                AS record_kind,'DIAGNOSTIC' AS evidence_class,h.lease_id AS attempt_id,
            CAST(NULL AS {uid}) AS parent_record_id,h.direction,h.started_at AS occurred_at,
            h.started_at AS recorded_at,'LIVE' AS capture_origin,
            CAST(NULL AS integer) AS http_status,
            CAST(NULL AS text) AS network_state,CAST(NULL AS text) AS protocol_verdict,
            CAST(NULL AS text) AS protocol_code,CAST(NULL AS text) AS business_result,
            CAST(NULL AS text) AS application_state,CAST(NULL AS boolean) AS late,
            h.reported_revision AS outcome_revision,
            h.request_wire_state AS body_state,h.request_wire_sha256 AS body_sha256,
            h.request_wire_bytes AS stored_bytes,CAST(NULL AS {uid}) AS body_record_id,
            CAST(NULL AS text) AS source_kind,
            CAST(NULL AS text) AS source_key,CAST(NULL AS text) AS source_version,
            h.response_record_id, 'request' AS side,h.peer_system,h.trace_id,h.reason_code
        FROM {p}integration_exchange_summary h
        UNION ALL SELECT h.response_record_id,h.id,
            CASE WHEN h.direction='INBOUND' THEN 'RESPONSE_PREPARED'
                 ELSE 'HTTP_RESPONSE_OBSERVED' END,
            'DIAGNOSTIC',h.lease_id,h.request_record_id,
            CASE WHEN h.direction='INBOUND' THEN 'OUTBOUND' ELSE 'INBOUND' END,
            COALESCE(h.late_response_at,h.response_observed_at,h.finished_at,h.started_at),
            COALESCE(h.late_response_at,h.response_observed_at,h.finished_at,h.started_at),
            'LIVE',h.protocol_http_status,h.attempt_state,h.protocol_verdict,h.protocol_code,
            h.reported_result,h.handling_outcome,h.late_response_at IS NOT NULL,h.reported_revision,
            h.response_wire_state,h.response_wire_sha256,h.response_wire_bytes,
            NULL,NULL,NULL,NULL,NULL,'response',h.peer_system,h.trace_id,h.reason_code
        FROM {p}integration_exchange_summary h WHERE h.response_record_id IS NOT NULL
    """)
    )
