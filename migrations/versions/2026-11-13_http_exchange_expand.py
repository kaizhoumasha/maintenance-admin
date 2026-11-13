"""Create HTTP exchange tables and durable reliable attempt evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "http_exchange_expand"
down_revision = "coarse_sorting_mock_round"
branch_labels = None
depends_on = None


def upgrade():
    schema = None if op.get_bind().dialect.name == "sqlite" else "enterprise_backend"
    op.create_table(
        "reliable_delivery_attempt",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("lease_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("exchange_id", sa.Uuid(), nullable=False),
        sa.Column("request_record_id", sa.Uuid(), nullable=False),
        sa.Column("response_record_id", sa.Uuid(), nullable=True),
        sa.Column("response_state", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("business_key", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("peer_endpoint", sa.Text(), nullable=False),
        sa.Column("traffic_source", sa.Text(), nullable=False),
        sa.Column("http_method", sa.Text(), nullable=False),
        sa.Column("endpoint_path", sa.Text(), nullable=False),
        sa.Column(
            "request_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "request_query",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("attempt_state", sa.Text(), nullable=False),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "message_id", "lease_id", name=op.f("reliable_delivery_attempt_pkey")
        ),
        sa.UniqueConstraint("id", name=op.f("reliable_delivery_attempt_id_key")),
        schema=schema,
    )
    op.create_table(
        "wms_http_exchange",
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("interaction_kind", sa.Text(), nullable=False),
        sa.Column("traffic_source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_method", sa.Text(), nullable=False),
        sa.Column("endpoint_path", sa.Text(), nullable=False),
        sa.Column("peer_endpoint", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column(
            "request_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "request_query",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire", sa.LargeBinary(), nullable=True),
        sa.Column("response_wire", sa.LargeBinary(), nullable=True),
        sa.Column(
            "request_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire_state", sa.Text(), nullable=False),
        sa.Column("response_wire_state", sa.Text(), nullable=False),
        sa.Column("request_wire_sha256", sa.Text(), nullable=True),
        sa.Column("response_wire_sha256", sa.Text(), nullable=True),
        sa.Column("request_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("response_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attempt_state", sa.Text(), nullable=False),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("protocol_code", sa.Text(), nullable=True),
        sa.Column("protocol_verdict", sa.Text(), nullable=True),
        sa.Column("handling_outcome", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reported_result", sa.Text(), nullable=True),
        sa.Column("replayed_from_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True),
        sa.Column("late_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "((request_wire_state IN ('FULL', 'PREVIEW') AND request_wire IS NOT NULL AND "
            "request_wire_sha256 IS NOT NULL AND request_wire_bytes IS NOT NULL AND "
            "request_wire_bytes = length(request_wire)) OR (request_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND request_wire IS NULL)) AND (request_wire_state = "
            "'FULL' OR request_payload IS NULL)",
            name=op.f("wms_http_exchange_request_body_check"),
        ),
        sa.CheckConstraint(
            "((response_wire_state IN ('FULL', 'PREVIEW') AND response_wire IS NOT NULL AND "
            "response_wire_sha256 IS NOT NULL AND response_wire_bytes IS NOT NULL AND "
            "response_wire_bytes = length(response_wire)) OR (response_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND response_wire IS NULL)) AND (response_wire_state = "
            "'FULL' OR response_payload IS NULL)",
            name=op.f("wms_http_exchange_response_body_check"),
        ),
        sa.CheckConstraint(
            "(attempt_state = 'OPEN') = (finished_at IS NULL)",
            name=op.f("wms_http_exchange_finish_check"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND', 'OUTBOUND')", name=op.f("wms_http_exchange_direction_check")
        ),
        sa.CheckConstraint(
            "interaction_kind IN ('DECISION', 'FACT', 'CALLBACK')",
            name=op.f("wms_http_exchange_interaction_kind_check"),
        ),
        sa.CheckConstraint(
            "traffic_source IN ('PEER', 'MOCK')",
            name=op.f("wms_http_exchange_traffic_source_check"),
        ),
        sa.CheckConstraint(
            "(message_id IS NULL) = (lease_id IS NULL)",
            name=op.f("wms_http_exchange_lease_pair_check"),
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599", name=op.f("wms_http_exchange_http_status_check")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("wms_http_exchange_pkey")),
        schema=schema,
    )
    op.create_index(
        "wms_http_exchange_lease_idx",
        "wms_http_exchange",
        ["message_id", "lease_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("lease_id IS NOT NULL"),
        sqlite_where=sa.text("lease_id IS NOT NULL"),
    )
    op.create_index(
        "wms_http_exchange_operation_id_idx",
        "wms_http_exchange",
        ["operation_id", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("operation_id IS NOT NULL"),
        sqlite_where=sa.text("operation_id IS NOT NULL"),
    )
    op.create_index(
        "wms_http_exchange_time_idx",
        "wms_http_exchange",
        ["started_at", "id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "wms_http_exchange_trace_id_idx",
        "wms_http_exchange",
        ["trace_id", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("trace_id IS NOT NULL"),
        sqlite_where=sa.text("trace_id IS NOT NULL"),
    )
    op.create_table(
        "ecs_http_exchange",
        sa.Column("command_code", sa.Text(), nullable=True),
        sa.Column("device_code", sa.Text(), nullable=True),
        sa.Column("task_type", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("interaction_kind", sa.Text(), nullable=False),
        sa.Column("traffic_source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_method", sa.Text(), nullable=False),
        sa.Column("endpoint_path", sa.Text(), nullable=False),
        sa.Column("peer_endpoint", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column(
            "request_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "request_query",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire", sa.LargeBinary(), nullable=True),
        sa.Column("response_wire", sa.LargeBinary(), nullable=True),
        sa.Column(
            "request_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire_state", sa.Text(), nullable=False),
        sa.Column("response_wire_state", sa.Text(), nullable=False),
        sa.Column("request_wire_sha256", sa.Text(), nullable=True),
        sa.Column("response_wire_sha256", sa.Text(), nullable=True),
        sa.Column("request_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("response_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attempt_state", sa.Text(), nullable=False),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("protocol_code", sa.Text(), nullable=True),
        sa.Column("protocol_verdict", sa.Text(), nullable=True),
        sa.Column("handling_outcome", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reported_result", sa.Text(), nullable=True),
        sa.Column("replayed_from_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True),
        sa.Column("late_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "((request_wire_state IN ('FULL', 'PREVIEW') AND request_wire IS NOT NULL AND "
            "request_wire_sha256 IS NOT NULL AND request_wire_bytes IS NOT NULL AND "
            "request_wire_bytes = length(request_wire)) OR (request_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND request_wire IS NULL)) AND (request_wire_state = "
            "'FULL' OR request_payload IS NULL)",
            name=op.f("ecs_http_exchange_request_body_check"),
        ),
        sa.CheckConstraint(
            "((response_wire_state IN ('FULL', 'PREVIEW') AND response_wire IS NOT NULL AND "
            "response_wire_sha256 IS NOT NULL AND response_wire_bytes IS NOT NULL AND "
            "response_wire_bytes = length(response_wire)) OR (response_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND response_wire IS NULL)) AND (response_wire_state = "
            "'FULL' OR response_payload IS NULL)",
            name=op.f("ecs_http_exchange_response_body_check"),
        ),
        sa.CheckConstraint(
            "(attempt_state = 'OPEN') = (finished_at IS NULL)",
            name=op.f("ecs_http_exchange_finish_check"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND', 'OUTBOUND')", name=op.f("ecs_http_exchange_direction_check")
        ),
        sa.CheckConstraint(
            "interaction_kind IN ('COMMAND', 'RESULT', 'EVENT', 'STATUS')",
            name=op.f("ecs_http_exchange_interaction_kind_check"),
        ),
        sa.CheckConstraint(
            "traffic_source IN ('PEER', 'MOCK')",
            name=op.f("ecs_http_exchange_traffic_source_check"),
        ),
        sa.CheckConstraint(
            "(message_id IS NULL) = (lease_id IS NULL)",
            name=op.f("ecs_http_exchange_lease_pair_check"),
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599", name=op.f("ecs_http_exchange_http_status_check")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("ecs_http_exchange_pkey")),
        schema=schema,
    )
    op.create_index(
        "ecs_http_exchange_command_code_idx",
        "ecs_http_exchange",
        ["command_code", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("command_code IS NOT NULL"),
        sqlite_where=sa.text("command_code IS NOT NULL"),
    )
    op.create_index(
        "ecs_http_exchange_device_code_idx",
        "ecs_http_exchange",
        ["device_code", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("device_code IS NOT NULL"),
        sqlite_where=sa.text("device_code IS NOT NULL"),
    )
    op.create_index(
        "ecs_http_exchange_lease_idx",
        "ecs_http_exchange",
        ["message_id", "lease_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("lease_id IS NOT NULL"),
        sqlite_where=sa.text("lease_id IS NOT NULL"),
    )
    op.create_index(
        "ecs_http_exchange_time_idx",
        "ecs_http_exchange",
        ["started_at", "id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ecs_http_exchange_trace_id_idx",
        "ecs_http_exchange",
        ["trace_id", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("trace_id IS NOT NULL"),
        sqlite_where=sa.text("trace_id IS NOT NULL"),
    )
    op.create_table(
        "transport_http_exchange",
        sa.Column("transport_task_id", sa.Text(), nullable=True),
        sa.Column("task_match_state", sa.Text(), nullable=False),
        sa.Column("outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("interaction_kind", sa.Text(), nullable=False),
        sa.Column("traffic_source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_method", sa.Text(), nullable=False),
        sa.Column("endpoint_path", sa.Text(), nullable=False),
        sa.Column("peer_endpoint", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column(
            "request_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_headers",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "request_query",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire", sa.LargeBinary(), nullable=True),
        sa.Column("response_wire", sa.LargeBinary(), nullable=True),
        sa.Column(
            "request_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "response_payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("request_wire_state", sa.Text(), nullable=False),
        sa.Column("response_wire_state", sa.Text(), nullable=False),
        sa.Column("request_wire_sha256", sa.Text(), nullable=True),
        sa.Column("response_wire_sha256", sa.Text(), nullable=True),
        sa.Column("request_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("response_wire_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attempt_state", sa.Text(), nullable=False),
        sa.Column("failure_kind", sa.Text(), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column("protocol_code", sa.Text(), nullable=True),
        sa.Column("protocol_verdict", sa.Text(), nullable=True),
        sa.Column("handling_outcome", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("reported_result", sa.Text(), nullable=True),
        sa.Column("replayed_from_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("lease_id", sa.Text(), nullable=True),
        sa.Column("receipt_id", sa.BigInteger(), nullable=True),
        sa.Column("late_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "((request_wire_state IN ('FULL', 'PREVIEW') AND request_wire IS NOT NULL AND "
            "request_wire_sha256 IS NOT NULL AND request_wire_bytes IS NOT NULL AND "
            "request_wire_bytes = length(request_wire)) OR (request_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND request_wire IS NULL)) AND (request_wire_state = "
            "'FULL' OR request_payload IS NULL)",
            name=op.f("transport_http_exchange_request_body_check"),
        ),
        sa.CheckConstraint(
            "((response_wire_state IN ('FULL', 'PREVIEW') AND response_wire IS NOT NULL AND "
            "response_wire_sha256 IS NOT NULL AND response_wire_bytes IS NOT NULL AND "
            "response_wire_bytes = length(response_wire)) OR (response_wire_state IN "
            "('UNAVAILABLE', 'PURGED') AND response_wire IS NULL)) AND (response_wire_state = "
            "'FULL' OR response_payload IS NULL)",
            name=op.f("transport_http_exchange_response_body_check"),
        ),
        sa.CheckConstraint(
            "(attempt_state = 'OPEN') = (finished_at IS NULL)",
            name=op.f("transport_http_exchange_finish_check"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND', 'OUTBOUND')",
            name=op.f("transport_http_exchange_direction_check"),
        ),
        sa.CheckConstraint(
            "interaction_kind IN ('SUBMIT', 'POSITION_CALLBACK', 'RESULT_CALLBACK')",
            name=op.f("transport_http_exchange_interaction_kind_check"),
        ),
        sa.CheckConstraint(
            "task_match_state IN ('MATCHED', 'UNMATCHED', 'UNKNOWN')",
            name=op.f("transport_http_exchange_task_match_check"),
        ),
        sa.CheckConstraint(
            "traffic_source IN ('PEER', 'MOCK')",
            name=op.f("transport_http_exchange_traffic_source_check"),
        ),
        sa.CheckConstraint(
            "(message_id IS NULL) = (lease_id IS NULL)",
            name=op.f("transport_http_exchange_lease_pair_check"),
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 100 AND 599",
            name=op.f("transport_http_exchange_http_status_check"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("transport_http_exchange_pkey")),
        schema=schema,
    )
    op.create_index(
        "transport_http_exchange_lease_idx",
        "transport_http_exchange",
        ["message_id", "lease_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("lease_id IS NOT NULL"),
        sqlite_where=sa.text("lease_id IS NOT NULL"),
    )
    op.create_index(
        "transport_http_exchange_time_idx",
        "transport_http_exchange",
        ["started_at", "id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "transport_http_exchange_trace_id_idx",
        "transport_http_exchange",
        ["trace_id", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("trace_id IS NOT NULL"),
        sqlite_where=sa.text("trace_id IS NOT NULL"),
    )
    op.create_index(
        "transport_http_exchange_transport_task_id_idx",
        "transport_http_exchange",
        ["transport_task_id", "started_at", "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("transport_task_id IS NOT NULL"),
        sqlite_where=sa.text("transport_task_id IS NOT NULL"),
    )


def downgrade():
    schema = None if op.get_bind().dialect.name == "sqlite" else "enterprise_backend"
    op.drop_table("transport_http_exchange", schema=schema)
    op.drop_table("ecs_http_exchange", schema=schema)
    op.drop_table("wms_http_exchange", schema=schema)
    op.drop_table("reliable_delivery_attempt", schema=schema)
