"""bin_roller_state

Revision ID: ac980eb32c52
Revises: a39a1988a81a
Create Date: 2026-11-01 23:46:30.835633+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "ac980eb32c52"
down_revision = "a39a1988a81a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.create_table(
        "bin_roller_scan_fact",
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("binding_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan_fact_pkey")),
        sa.UniqueConstraint("digest", name=op.f("bin_roller_scan_fact_digest_key")),
        schema=schema,
    )
    op.create_table(
        "bin_roller_scan_arrival",
        sa.Column("business_key", sa.String(length=64), nullable=False),
        sa.Column("first_digest", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("point", sa.String(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("previous_arrival_id", sa.String(), nullable=True),
        sa.Column("separator_command_code", sa.String(), nullable=True),
        sa.Column("return_round_id", sa.String(), nullable=True),
        sa.Column("delegated", sa.Boolean(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.Column("consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan_arrival_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "business_key",
            name=op.f("bin_roller_scan_arrival_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_scan2_release",
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("call_key", sa.String(), nullable=False),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("consumed_result_id", sa.String(), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_scan2_release_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "arrival_id",
            name=op.f("bin_roller_scan2_release_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_position_fact",
        sa.Column("object_kind", sa.String(), nullable=False),
        sa.Column("object_code", sa.String(), nullable=False),
        sa.Column("location_code", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_position_fact_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "source_kind",
            "source_id",
            "source_revision",
            name=op.f("bin_roller_position_fact_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_current_object",
        "bin_roller_position_fact",
        ["owner_domain", "work_line_id", "object_kind", "object_code"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("is_current"),
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "bin_roller_current_position",
        "bin_roller_position_fact",
        ["owner_domain", "work_line_id", "object_kind", "location_code"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("is_current"),
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "bin_roller_rack_cycle",
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("resources", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("release_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_rack_cycle_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "source_ref",
            "rack_id",
            name=op.f("bin_roller_rack_cycle_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_rack_action",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=True),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_face", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("position_revision", sa.BigInteger(), nullable=True),
        sa.Column("acceptance_basis", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_rack_action_pkey")),
        sa.UniqueConstraint(
            "client_request_id", name=op.f("bin_roller_rack_action_client_request_id_key")
        ),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "kind",
            "source_ref",
            name=op.f("bin_roller_rack_action_owner_domain_key"),
        ),
        sa.UniqueConstraint(
            "transport_task_id", name=op.f("bin_roller_rack_action_transport_task_id_key")
        ),
        schema=schema,
    )
    op.create_index(
        "bin_roller_cycle_entry_departure",
        "bin_roller_rack_action",
        ["owner_domain", "work_line_id", "cycle_id", "kind"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("kind IN ('CTU01', 'CTU03')"),
        postgresql_where=sa.text("kind IN ('CTU01', 'CTU03')"),
    )
    op.create_table(
        "bin_roller_face_service",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_face_service_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            name=op.f("bin_roller_face_service_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_supply_demand",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("change_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("cycle_id", sa.String(), nullable=True),
        sa.Column("service_id", sa.String(), nullable=True),
        sa.Column("withdrawn", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_supply_demand_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            name=op.f("bin_roller_supply_demand_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_feed",
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("feed_complete", sa.Boolean(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_feed_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "service_id",
            name=op.f("bin_roller_feed_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_feed_segment",
        sa.Column("feed_id", sa.String(), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("operation_id", sa.String(), nullable=True),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_feed_segment_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "feed_id",
            "segment_index",
            name=op.f("bin_roller_feed_segment_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_return_round",
        sa.Column("scan3_arrival_id", sa.String(), nullable=False),
        sa.Column("scan4_arrival_id", sa.String(), nullable=True),
        sa.Column("bin_code", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_return_round_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "scan3_arrival_id",
            name=op.f("bin_roller_return_round_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_return_opportunity",
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ended", sa.Boolean(), nullable=False),
        sa.Column("end_reason", sa.String(), nullable=True),
        sa.Column("end_evidence", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_return_opportunity_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "service_id",
            "source_ref",
            name=op.f("bin_roller_return_opportunity_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_return_batch",
        sa.Column("opportunity_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("transport_operation_id", sa.String(), nullable=True),
        sa.Column("consumed_revision", sa.BigInteger(), nullable=True),
        sa.Column("client_request_id", sa.String(), nullable=True),
        sa.Column("transport_task_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_return_batch_pkey")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "opportunity_id",
            "source_ref",
            name=op.f("bin_roller_return_batch_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_drain",
        sa.Column("input_kind", sa.String(), nullable=False),
        sa.Column("input_ref", sa.String(), nullable=False),
        sa.Column("input_snapshot", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("request", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("unused_racks", sa.JSON(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("due_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_drain_pkey")),
        sa.UniqueConstraint("operation_id", name=op.f("bin_roller_drain_operation_id_key")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "input_kind",
            "input_ref",
            name=op.f("bin_roller_drain_owner_domain_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "bin_roller_departure",
        sa.Column("cycle_id", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("due_at", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_domain", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("bin_roller_departure_pkey")),
        sa.UniqueConstraint("operation_id", name=op.f("bin_roller_departure_operation_id_key")),
        sa.UniqueConstraint(
            "owner_domain",
            "work_line_id",
            "cycle_id",
            "source_ref",
            name=op.f("bin_roller_departure_owner_domain_key"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_table("bin_roller_departure", schema=schema)
    op.drop_table("bin_roller_drain", schema=schema)
    op.drop_table("bin_roller_return_batch", schema=schema)
    op.drop_table("bin_roller_return_opportunity", schema=schema)
    op.drop_table("bin_roller_return_round", schema=schema)
    op.drop_table("bin_roller_feed_segment", schema=schema)
    op.drop_table("bin_roller_feed", schema=schema)
    op.drop_table("bin_roller_supply_demand", schema=schema)
    op.drop_table("bin_roller_face_service", schema=schema)
    op.drop_table("bin_roller_rack_action", schema=schema)
    op.drop_table("bin_roller_rack_cycle", schema=schema)
    op.drop_table("bin_roller_position_fact", schema=schema)
    op.drop_table("bin_roller_scan2_release", schema=schema)
    op.drop_table("bin_roller_scan_arrival", schema=schema)
    op.drop_table("bin_roller_scan_fact", schema=schema)
