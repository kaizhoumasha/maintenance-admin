"""smt_manual_state

Revision ID: 0177313880f9
Revises: ac980eb32c52
Create Date: 2026-11-01 23:46:31.024531+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0177313880f9"
down_revision = "ac980eb32c52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.create_table(
        "smt_manual_task",
        sa.Column("task_id", sa.String(length=100), nullable=False),
        sa.Column("task_type", sa.String(), nullable=False),
        sa.Column("business_type", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("issued_operation_id", sa.String(), nullable=False),
        sa.Column("issued_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("issued_data", sa.JSON(), nullable=False),
        sa.Column("queue_revision", sa.BigInteger(), nullable=False),
        sa.Column("dispatch_sequence", sa.BigInteger(), nullable=False),
        sa.Column("not_before", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("prepare_operation_id", sa.String(), nullable=True),
        sa.Column("prepare_message_id", sa.BigInteger(), nullable=True),
        sa.Column("prepare_accepted", sa.Boolean(), nullable=False),
        sa.Column("prepare_response_timestamp", sa.BigInteger(), nullable=True),
        sa.Column("prepare_accepted_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("confirmation_operation_id", sa.String(), nullable=True),
        sa.Column("received_work", sa.JSON(), nullable=False),
        sa.Column("target_rack", sa.JSON(none_as_null=True), nullable=True),
        sa.PrimaryKeyConstraint("task_id", name=op.f("smt_manual_task_pkey")),
        schema=schema,
    )
    op.create_index(
        "smt_manual_execution_slot",
        "smt_manual_task",
        ["work_line_id"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("status IN ('PREPARING', 'EXECUTING')"),
        sqlite_where=sa.text("status IN ('PREPARING', 'EXECUTING')"),
    )
    op.create_table(
        "smt_manual_task_input",
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("unapplied_reason", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint(
            "operation", "operation_id", name=op.f("smt_manual_task_input_pkey")
        ),
        schema=schema,
    )
    op.create_table(
        "smt_manual_task_confirmation",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_task_confirmation_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_task_plan",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "plan_revision", name=op.f("smt_manual_task_plan_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_task_member",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=False),
        sa.Column("slot_id", sa.String(), nullable=False),
        sa.Column("member_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("cancelled_operation_id", sa.String(), nullable=True),
        sa.Column("rack_work_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("smt_manual_task_member_pkey")),
        sa.UniqueConstraint(
            "task_id",
            "plan_revision",
            "kind",
            "rack_id",
            "rack_face",
            "slot_id",
            name=op.f("smt_manual_task_member_task_id_key"),
        ),
        schema=schema,
    )
    op.create_table(
        "smt_manual_bin",
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("raw_bin_code", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=True),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("admission_operation_id", sa.String(), nullable=True),
        sa.Column("completed_operation_id", sa.String(), nullable=True),
        sa.Column("completed_result", sa.String(), nullable=True),
        sa.Column("command_code", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("arrival_id", name=op.f("smt_manual_bin_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_admission",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("response", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("saved_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_admission_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_completion",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("bin_code", sa.String(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("completed_at", sa.BigInteger(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("arrival_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("original_operation_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_completion_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_completion",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("rack_face", sa.String(), nullable=True),
        sa.Column("completed_at", sa.BigInteger(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "operation_id", "operation", name=op.f("smt_manual_rack_completion_pkey")
        ),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("rack_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("location_code", sa.String(), nullable=False),
        sa.Column("initial_face", sa.String(), nullable=False),
        sa.Column("caller_workline_id", sa.String(), nullable=False),
        sa.Column("station_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("smt_manual_rack_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_action",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("client_request_id", sa.String(), nullable=False),
        sa.Column("transport_task_id", sa.String(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("target_face", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("outcome_revision", sa.BigInteger(), nullable=True),
        sa.Column("observation_revision", sa.BigInteger(), nullable=True),
        sa.Column("actual_position", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("actual_face", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("smt_manual_rack_action_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_report",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("action_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_rack_report_pkey")),
        schema=schema,
    )
    op.create_table(
        "smt_manual_rack_departure",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("rack_work_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("decision", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("wait_until_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("smt_manual_rack_departure_pkey")),
        schema=schema,
    )


def downgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_table("smt_manual_rack_departure", schema=schema)
    op.drop_table("smt_manual_rack_report", schema=schema)
    op.drop_table("smt_manual_rack_action", schema=schema)
    op.drop_table("smt_manual_rack", schema=schema)
    op.drop_table("smt_manual_rack_completion", schema=schema)
    op.drop_table("smt_manual_completion", schema=schema)
    op.drop_table("smt_manual_admission", schema=schema)
    op.drop_table("smt_manual_bin", schema=schema)
    op.drop_table("smt_manual_task_member", schema=schema)
    op.drop_table("smt_manual_task_plan", schema=schema)
    op.drop_table("smt_manual_task_confirmation", schema=schema)
    op.drop_table("smt_manual_task_input", schema=schema)
    op.drop_table("smt_manual_task", schema=schema)
