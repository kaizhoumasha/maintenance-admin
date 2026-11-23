"""Add indexes for pending and due queue scans."""

import sqlalchemy as sa
from alembic import op

from app.database import SCHEMA

revision = "pending_scan_indexes_v1"
down_revision = "smt_auto_manual_bypass_v1"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "smt_auto_bin_work",
        "smt_auto_bin_work_pending_idx",
        ("id",),
        "pending = 1",
        "pending",
    ),
    (
        "smt_auto_bin_work",
        "smt_auto_bin_work_release_idx",
        ("id",),
        "release_command_code IS NOT NULL AND release_consumed_at_ms IS NULL",
        "release_command_code IS NOT NULL AND release_consumed_at_ms IS NULL",
    ),
    (
        "smt_auto_inbound_plate",
        "smt_auto_inbound_plate_pending_idx",
        ("id",),
        "pending = 1",
        "pending",
    ),
    (
        "smt_auto_inbound_plate",
        "smt_auto_inbound_plate_report_idx",
        ("id",),
        "report_message_id IS NOT NULL AND report_consumed_at_ms IS NULL",
        "report_message_id IS NOT NULL AND report_consumed_at_ms IS NULL",
    ),
    (
        "smt_auto_outbound_plate",
        "smt_auto_outbound_plate_pending_idx",
        ("id",),
        "pending = 1",
        "pending",
    ),
    (
        "smt_auto_outbound_plate",
        "smt_auto_outbound_plate_report_idx",
        ("id",),
        "report_message_id IS NOT NULL AND report_consumed_at_ms IS NULL "
        "AND direct_obligations_closed_at_ms IS NULL",
        "report_message_id IS NOT NULL AND report_consumed_at_ms IS NULL "
        "AND direct_obligations_closed_at_ms IS NULL",
    ),
    (
        "smt_auto_outbound_source_member",
        "smt_auto_outbound_source_member_terminal_idx",
        ("id",),
        "arrived_at_ms IS NOT NULL AND terminal_basis IS NULL",
        "arrived_at_ms IS NOT NULL AND terminal_basis IS NULL",
    ),
    (
        "coarse_sorting_job",
        "coarse_sorting_job_pending_due_idx",
        ("next_check_at", "id"),
        "pending IS 1",
        "pending",
    ),
)


def upgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    for table, name, columns, sqlite_predicate, postgres_predicate in INDEXES:
        op.create_index(
            name,
            table,
            list(columns),
            schema=schema,
            sqlite_where=sa.text(sqlite_predicate),
            postgresql_where=sa.text(postgres_predicate),
        )


def downgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    for table, name, *_ in reversed(INDEXES):
        op.drop_index(name, table_name=table, schema=schema)
