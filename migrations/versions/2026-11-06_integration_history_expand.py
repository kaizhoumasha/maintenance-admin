"""Expand integration evidence storage without changing the active writer."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError
from sqlalchemy.dialects import postgresql

from migrations.integration_history_views_v1 import create_views, drop_views

revision = "integrationhistoryexpand"
down_revision = "coarserackstage3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    prefix = f"{schema}." if schema else ""
    op.create_table(
        "integration_exchange",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("peer_system", sa.String(length=3), nullable=True),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("interaction_kind", sa.String(length=8), nullable=False),
        sa.Column("operation", sa.Text(), nullable=True),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("message_type", sa.Text(), nullable=True),
        sa.Column("identity_kind", sa.String(length=32), nullable=False),
        sa.Column("identity_value", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=True),
        sa.Column("endpoint_path", sa.Text(), nullable=True),
        sa.Column("owner_domain", sa.Text(), nullable=True),
        sa.Column("device_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_origin", sa.String(length=8), nullable=False),
        sa.Column("coverage", sa.String(length=8), nullable=False),
        sa.Column("request_record_id", sa.Uuid(), nullable=True),
        sa.Column("ack_record_id", sa.Uuid(), nullable=True),
        sa.Column("current_response_record_id", sa.Uuid(), nullable=True),
        sa.Column("command_code", sa.String(length=36), nullable=True),
        sa.Column("call_key", sa.Text(), nullable=True),
        sa.Column("call_key_digest", sa.String(length=64), nullable=True),
        sa.Column("input_raw", sa.LargeBinary(), nullable=True),
        sa.Column("task_type", sa.Text(), nullable=True),
        sa.Column(
            "route",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column(
            "delivery_snapshot",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("delivery_status", sa.String(length=16), nullable=True),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hold_reason", sa.String(length=64), nullable=True),
        sa.Column("blocked", sa.Boolean(), nullable=True),
        sa.Column("needs_reconciliation", sa.Boolean(), nullable=True),
        sa.Column("applied_result_id", sa.String(length=64), nullable=True),
        sa.Column("receipt_id", sa.Integer(), nullable=True),
        sa.Column("receipt_active", sa.Boolean(), nullable=True),
        sa.Column("raw_digest", sa.String(length=64), nullable=True),
        sa.Column("effective_digest", sa.String(length=64), nullable=True),
        sa.Column("compare_mode", sa.String(length=9), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("resp_timestamp", sa.BigInteger(), nullable=True),
        sa.Column(
            "event_envelope",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "event_context",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "application_ref",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column(
            "diagnostic",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "(role='ECS_COMMAND' AND command_code IS NOT NULL AND owner_domain IS"
            " NOT NULL AND call_key IS NOT NULL AND call_key_digest IS NOT NULL "
            "AND input_raw IS NOT NULL AND status IS NOT NULL AND delivery_status"
            " IS NOT NULL) OR (role<>'ECS_COMMAND' AND command_code IS NULL AND "
            "call_key IS NULL AND call_key_digest IS NULL AND status IS NULL AND "
            "delivery_status IS NULL)",
            name=op.f("integration_exchange_command_check"),
        ),
        sa.CheckConstraint(
            "(role='RECEIPT' AND receipt_id IS NOT NULL AND receipt_active IS NOT"
            " NULL AND operation IS NOT NULL AND operation_id IS NOT NULL AND "
            "raw_digest IS NOT NULL AND compare_mode IS NOT NULL AND "
            "((compare_mode='RAW' AND effective_digest IS NULL) OR "
            "(compare_mode='EFFECTIVE' AND effective_digest IS NOT NULL))) OR "
            "(role<>'RECEIPT' AND receipt_id IS NULL AND receipt_active IS NULL "
            "AND compare_mode IS NULL)",
            name=op.f("integration_exchange_receipt_check"),
        ),
        sa.CheckConstraint(
            "capture_origin IN ('LIVE','BACKFILL')",
            name=op.f("integration_exchange_capture_origin_check"),
        ),
        sa.CheckConstraint(
            "coverage IN ('DECLARED','PARTIAL','LEGACY')",
            name=op.f("integration_exchange_coverage_check"),
        ),
        sa.CheckConstraint(
            "delivery_status IS NULL OR delivery_status IN "
            "('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED','ABANDONED')",
            name=op.f("integration_exchange_command_delivery_check"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND')", name=op.f("integration_exchange_direction_check")
        ),
        sa.CheckConstraint(
            "interaction_kind IN ('COMMAND','EVENT','REQUEST','CALLBACK')",
            name=op.f("integration_exchange_kind_check"),
        ),
        sa.CheckConstraint(
            "peer_system IS NULL OR peer_system IN ('ECS','WMS')",
            name=op.f("integration_exchange_peer_check"),
        ),
        sa.CheckConstraint(
            "role IN ('ECS_COMMAND','RECEIPT','OUTGOING','DIAGNOSTIC')",
            name=op.f("integration_exchange_role_check"),
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN "
            "('QUEUED','DISPATCHING','WAITING_RESULT','RECONCILING','SUCCEEDED',"
            "'FAILED','REJECTED','EXPIRED','HELD')",
            name=op.f("integration_exchange_command_status_check"),
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("integration_exchange_schema_version_check")
        ),
        sa.ForeignKeyConstraint(
            ["ack_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_exchange_ack_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["current_response_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_exchange_current_response_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["request_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_exchange_request_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("integration_exchange_pkey")),
        sa.UniqueConstraint("command_code", name=op.f("integration_exchange_command_code_key")),
        sa.UniqueConstraint("receipt_id", name=op.f("integration_exchange_receipt_id_key")),
        schema=schema,
    )
    op.create_index(
        "integration_exchange_active_receipt_key",
        "integration_exchange",
        ["operation", "operation_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("role='RECEIPT' AND receipt_active"),
        sqlite_where=sa.text("role='RECEIPT' AND receipt_active"),
    )
    op.create_index(
        "integration_exchange_call_key",
        "integration_exchange",
        ["owner_domain", "call_key_digest"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("role='ECS_COMMAND'"),
        sqlite_where=sa.text("role='ECS_COMMAND'"),
    )
    op.create_index(
        "integration_exchange_device_idx",
        "integration_exchange",
        ["device_code"],
        unique=False,
        schema=schema,
        postgresql_using="hash",
    )
    op.create_index(
        "integration_exchange_identity_idx",
        "integration_exchange",
        ["identity_value"],
        unique=False,
        schema=schema,
        postgresql_using="hash",
    )
    op.create_index(
        "integration_exchange_time_idx",
        "integration_exchange",
        ["created_at", "id"],
        unique=False,
        schema=schema,
    )
    op.create_table(
        "integration_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange_id", sa.Uuid(), nullable=False),
        sa.Column("record_kind", sa.String(length=32), nullable=False),
        sa.Column("evidence_class", sa.String(length=10), nullable=False),
        sa.Column("event_key", sa.String(length=256), nullable=False),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("parent_record_id", sa.Uuid(), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_origin", sa.String(length=8), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("network_state", sa.String(length=32), nullable=True),
        sa.Column("protocol_verdict", sa.String(length=32), nullable=True),
        sa.Column("protocol_code", sa.Text(), nullable=True),
        sa.Column("business_result", sa.Text(), nullable=True),
        sa.Column("application_state", sa.Text(), nullable=True),
        sa.Column("late", sa.Boolean(), nullable=True),
        sa.Column("outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("raw_body", sa.LargeBinary(), nullable=True),
        sa.Column("body_record_id", sa.Uuid(), nullable=True),
        sa.Column("body_sha256", sa.String(length=64), nullable=True),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=True),
        sa.Column("body_state", sa.String(length=16), nullable=False),
        sa.Column(
            "metadata",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("source_kind", sa.String(length=64), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.Column("semantic_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "effective_payload",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
        sa.Column("response_record_id", sa.Uuid(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=True),
        sa.Column("ecs_attempt_outcome", sa.String(length=32), nullable=True),
        sa.Column("attempt_revision", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "body_state IN ('FULL','PREVIEW','LEGACY_REENCODED','UNAVAILABLE','PURGED')",
            name=op.f("integration_record_body_state_check"),
        ),
        sa.CheckConstraint(
            "capture_origin IN ('LIVE','BACKFILL')",
            name=op.f("integration_record_capture_origin_check"),
        ),
        sa.CheckConstraint(
            "evidence_class IN ('CORE','DIAGNOSTIC')",
            name=op.f("integration_record_evidence_class_check"),
        ),
        sa.CheckConstraint(
            "direction IN ('INBOUND','OUTBOUND','INTERNAL')",
            name=op.f("integration_record_direction_check"),
        ),
        sa.CheckConstraint(
            "raw_body IS NULL OR (body_record_id IS NULL AND body_sha256 IS NOT "
            "NULL AND stored_bytes IS NOT NULL AND stored_bytes=length(raw_body) "
            "AND body_state IN ('FULL','PREVIEW','LEGACY_REENCODED'))",
            name=op.f("integration_record_body_check"),
        ),
        sa.CheckConstraint(
            "record_kind IN "
            "('REQUEST_REGISTERED','SEND_ATTEMPT_OPENED','HTTP_RESPONSE_OBSERVED','SEND_ATTEMPT_FINISHED','ECS_ATTEMPT_CONCLUSION','CALLBACK_RECEIVED','RESPONSE_PREPARED','RESULT_EVIDENCE','RESULT_APPLIED','RESULT_REJECTED','DELIVERY_TRANSITION','BUSINESS_CONSUMED','LEGACY_SNAPSHOT')",
            name=op.f("integration_record_kind_check"),
        ),
        sa.CheckConstraint(
            "record_kind<>'ECS_ATTEMPT_CONCLUSION' OR (attempt_id IS NOT NULL AND"
            " attempt_revision > 0 AND ecs_attempt_outcome IS NOT NULL)",
            name=op.f("integration_record_attempt_check"),
        ),
        sa.CheckConstraint(
            "record_kind<>'RESULT_EVIDENCE' OR (semantic_digest IS NOT NULL AND "
            "effective_payload IS NOT NULL AND response_record_id IS NOT NULL AND"
            " applied IS NOT NULL)",
            name=op.f("integration_record_result_check"),
        ),
        sa.CheckConstraint(
            "body_record_id IS NULL OR body_record_id<>id",
            name=op.f("integration_record_body_reference_check"),
        ),
        sa.ForeignKeyConstraint(
            ["body_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_record_body_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            [prefix + "integration_exchange.id"],
            name=op.f("integration_record_exchange_id_fkey"),
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["parent_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_record_parent_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["response_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_record_response_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("integration_record_pkey")),
        sa.UniqueConstraint(
            "exchange_id",
            "attempt_id",
            "attempt_revision",
            name="integration_record_revision_key",
        ),
        sa.UniqueConstraint("exchange_id", "event_key", name="integration_record_event_key"),
        sa.UniqueConstraint("exchange_id", "semantic_digest", name="integration_record_result_key"),
        sa.UniqueConstraint(
            "source_kind",
            "source_key",
            "source_version",
            name=op.f("integration_record_source_kind_key"),
        ),
        schema=schema,
    )
    op.create_index(
        "integration_record_attempt_idx",
        "integration_record",
        ["exchange_id", "attempt_id", "record_kind"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "integration_record_time_idx",
        "integration_record",
        ["exchange_id", "recorded_at", "id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "integration_record_retention_idx",
        "integration_record",
        ["evidence_class", "occurred_at", "id"],
        unique=False,
        schema=schema,
    )
    for column in ("body_record_id", "response_record_id", "parent_record_id"):
        op.create_index(
            f"integration_record_{column}_idx",
            "integration_record",
            [column],
            schema=schema,
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
            sqlite_where=sa.text(f"{column} IS NOT NULL"),
        )
    op.create_table(
        "integration_reference",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exchange_id", sa.Uuid(), nullable=False),
        sa.Column("reference_kind", sa.String(length=32), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("reference_value", sa.Text(), nullable=False),
        sa.Column("value_digest", sa.String(length=64), nullable=False),
        sa.Column("relation_role", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=True),
        sa.Column("related_exchange_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(reference_kind='EXCHANGE' AND related_exchange_id IS NOT NULL) OR "
            "(reference_kind<>'EXCHANGE' AND related_exchange_id IS NULL)",
            name=op.f("integration_reference_related_check"),
        ),
        sa.ForeignKeyConstraint(
            ["exchange_id"],
            [prefix + "integration_exchange.id"],
            name=op.f("integration_reference_exchange_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["related_exchange_id"],
            [prefix + "integration_exchange.id"],
            name=op.f("integration_reference_related_exchange_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"],
            [prefix + "integration_record.id"],
            name=op.f("integration_reference_source_record_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("integration_reference_pkey")),
        schema=schema,
    )
    op.create_index(
        "integration_reference_lookup_idx",
        "integration_reference",
        ["reference_kind", "namespace", "value_digest"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "integration_reference_root_key",
        "integration_reference",
        ["exchange_id", "reference_kind", "namespace", "value_digest", "relation_role"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("source_record_id IS NULL"),
        sqlite_where=sa.text("source_record_id IS NULL"),
    )
    op.create_index(
        "integration_reference_source_key",
        "integration_reference",
        [
            "exchange_id",
            "reference_kind",
            "namespace",
            "value_digest",
            "relation_role",
            "source_record_id",
        ],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("source_record_id IS NOT NULL"),
        sqlite_where=sa.text("source_record_id IS NOT NULL"),
    )
    if schema:
        op.create_foreign_key(
            op.f("integration_exchange_ack_record_id_fkey"),
            "integration_exchange",
            "integration_record",
            ["ack_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_exchange_current_response_record_id_fkey"),
            "integration_exchange",
            "integration_record",
            ["current_response_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_exchange_request_record_id_fkey"),
            "integration_exchange",
            "integration_record",
            ["request_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_record_body_record_id_fkey"),
            "integration_record",
            "integration_record",
            ["body_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_record_parent_record_id_fkey"),
            "integration_record",
            "integration_record",
            ["parent_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_record_response_record_id_fkey"),
            "integration_record",
            "integration_record",
            ["response_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        op.create_foreign_key(
            op.f("integration_reference_source_record_id_fkey"),
            "integration_reference",
            "integration_record",
            ["source_record_id"],
            ["id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )

    if schema:
        bind.execute(
            sa.schema.CreateSequence(sa.Sequence("integration_receipt_id_seq", schema=schema))
        )
        highwater = bind.scalar(
            sa.text("SELECT last_value FROM enterprise_backend.callback_receipt_id_seq")
        )
        bind.execute(
            sa.text(
                "SELECT setval('enterprise_backend.integration_receipt_id_seq', "
                "GREATEST(:highwater, COALESCE((SELECT MAX(id) FROM callback_receipt), 0), 1))"
            ),
            {"highwater": highwater},
        )
    else:
        op.execute(
            "CREATE TABLE _integration_receipt_sequence (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        op.execute(
            "INSERT INTO _integration_receipt_sequence (id) SELECT MAX(id) FROM "
            "callback_receipt HAVING MAX(id) IS NOT NULL"
        )
        op.execute("DELETE FROM _integration_receipt_sequence")
    for name, columns in (
        (
            "reliable_message",
            (
                "exchange_id",
                "request_record_id",
                "terminal_response_record_id",
                "last_response_record_id",
            ),
        ),
        (
            "transport_task",
            (
                "submission_exchange_id",
                "acceptance_request_record_id",
                "acceptance_response_record_id",
            ),
        ),
    ):
        with op.batch_alter_table(name, schema=schema) as batch:
            for column in columns:
                batch.add_column(sa.Column(column, sa.Uuid(), nullable=True))
                target = (
                    "integration_exchange"
                    if column.endswith("exchange_id")
                    else "integration_record"
                )
                batch.create_foreign_key(
                    f"{name}_{column}_fkey",
                    target,
                    [column],
                    ["id"],
                    referent_schema=schema,
                    ondelete="RESTRICT",
                )

    for name, columns in (
        (
            "integration_exchange",
            ("request_record_id", "ack_record_id", "current_response_record_id"),
        ),
        ("integration_reference", ("source_record_id", "related_exchange_id")),
        (
            "reliable_message",
            (
                "exchange_id",
                "request_record_id",
                "terminal_response_record_id",
                "last_response_record_id",
            ),
        ),
        (
            "transport_task",
            (
                "submission_exchange_id",
                "acceptance_request_record_id",
                "acceptance_response_record_id",
            ),
        ),
    ):
        for column in columns:
            op.create_index(
                f"{name}_{column}_idx",
                name,
                [column],
                schema=schema,
                postgresql_where=sa.text(f"{column} IS NOT NULL"),
                sqlite_where=sa.text(f"{column} IS NOT NULL"),
            )
    op.create_index(
        "integration_reference_exchange_id_idx",
        "integration_reference",
        ["exchange_id"],
        schema=schema,
    )

    create_views(bind)


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if bind.scalar(sa.text("SELECT COUNT(*) FROM integration_exchange")):
        raise CommandError("refusing to discard integration history; reconcile before downgrade")
    drop_views(bind)
    for name, columns in (
        (
            "transport_task",
            (
                "submission_exchange_id",
                "acceptance_request_record_id",
                "acceptance_response_record_id",
            ),
        ),
        (
            "reliable_message",
            (
                "exchange_id",
                "request_record_id",
                "terminal_response_record_id",
                "last_response_record_id",
            ),
        ),
    ):
        with op.batch_alter_table(name, schema=schema) as batch:
            for column in columns:
                batch.drop_index(f"{name}_{column}_idx")
                batch.drop_constraint(f"{name}_{column}_fkey", type_="foreignkey")
                batch.drop_column(column)
    if schema:
        for column in ("request_record_id", "ack_record_id", "current_response_record_id"):
            op.drop_constraint(
                f"integration_exchange_{column}_fkey",
                "integration_exchange",
                schema=schema,
                type_="foreignkey",
            )
    op.drop_table("integration_reference", schema=schema)
    op.drop_table("integration_record", schema=schema)
    op.drop_table("integration_exchange", schema=schema)
    if schema:
        bind.execute(
            sa.schema.DropSequence(sa.Sequence("integration_receipt_id_seq", schema=schema))
        )
    else:
        op.execute("DROP TABLE _integration_receipt_sequence")
