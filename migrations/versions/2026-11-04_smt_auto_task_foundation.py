"""Add the shared SMT AUTO task foundation."""

import sqlalchemy as sa
from alembic import op

revision = "smtautofoundation"
down_revision = "smtmanualoprename"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "smt_auto_task",
        sa.Column("task_id", sa.String(100), primary_key=True),
        sa.Column("task_type", sa.String(20), nullable=False, server_default="AUTO"),
        sa.Column("business_type", sa.String(20), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(100), nullable=False),
        sa.Column("issued_operation_id", sa.String(200), nullable=False),
        sa.Column("issued_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("issued_data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("queue_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("dispatch_sequence", sa.BigInteger(), nullable=False),
        sa.Column("not_before_ms", sa.BigInteger()),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("prepare_accepted_at_ms", sa.BigInteger()),
        sa.Column("confirmation_next_check_at_ms", sa.BigInteger()),
        sa.Column("confirmation_basis", sa.JSON()),
        sa.Column("closed_at_ms", sa.BigInteger()),
        sa.Column("cancel_reason", sa.String(200)),
        sa.Column("direct_obligations_closed_at_ms", sa.BigInteger()),
        sa.Column("details_cleaned_at_ms", sa.BigInteger()),
        sa.Column("details_cleaned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_check_at_ms", sa.BigInteger()),
        sa.CheckConstraint("task_type = 'AUTO'", name="smt_auto_task_type"),
        sa.CheckConstraint(
            "business_type IN ('inbound','outbound')", name="smt_auto_task_business_type"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','PREPARING','EXECUTING','COMPLETED','CANCELLED')",
            name="smt_auto_task_status",
        ),
        sa.CheckConstraint(
            "queue_revision >= 1 AND plan_revision >= 0", name="smt_auto_task_revisions"
        ),
    )
    op.create_index(
        "smt_auto_task_queue",
        "smt_auto_task",
        ["work_line_id", "dispatch_sequence", "issued_timestamp", "task_id"],
    )
    op.create_index(
        "smt_auto_task_execution_slot",
        "smt_auto_task",
        ["work_line_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PREPARING','EXECUTING')"),
        sqlite_where=sa.text("status IN ('PREPARING','EXECUTING')"),
    )
    op.create_table(
        "smt_auto_task_input",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id"), nullable=False
        ),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(200), nullable=False),
        sa.Column("request_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("first_received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("effective_digest", sa.String(128), nullable=False),
        sa.Column("contract_version", sa.String(50), nullable=False),
        sa.Column("effective_request", sa.JSON(), nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False),
        sa.Column("block_reason", sa.String(100)),
        sa.Column("business_outcome", sa.JSON()),
        sa.Column("outcome_recorded_at_ms", sa.BigInteger()),
        sa.Column("requested_revision", sa.BigInteger()),
        sa.Column("applied_plan_revision", sa.BigInteger()),
        sa.Column("applied_queue_revision", sa.BigInteger()),
        sa.Column("canonical_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id")),
        sa.Column("supply_change_id", sa.String(200)),
        sa.Column("supply_change", sa.JSON()),
        sa.UniqueConstraint("operation", "operation_id", name="smt_auto_input_identity"),
        sa.CheckConstraint(
            "disposition IN ('BLOCKED','REJECTED','APPLIED','DUPLICATE')",
            name="smt_auto_input_disposition",
        ),
    )
    op.create_index(
        "smt_auto_input_plan",
        "smt_auto_task_input",
        ["task_id", "applied_plan_revision"],
        unique=True,
        postgresql_where=sa.text("applied_plan_revision IS NOT NULL"),
        sqlite_where=sa.text("applied_plan_revision IS NOT NULL"),
    )
    op.create_table(
        "smt_auto_task_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id"), nullable=False
        ),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(200), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_plan_revision", sa.BigInteger()),
        sa.Column("request_data", sa.JSON(), nullable=False),
        sa.Column("response_snapshot", sa.JSON()),
        sa.Column("response_recorded_at_ms", sa.BigInteger()),
        sa.Column("consumed_at_ms", sa.BigInteger()),
        sa.Column("unresolved", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("completion_result", sa.JSON()),
        sa.Column("retry_after_ms", sa.BigInteger()),
        sa.Column("next_eligible_at_ms", sa.BigInteger()),
        sa.UniqueConstraint("operation", "operation_id", name="smt_auto_request_identity"),
        sa.UniqueConstraint("message_id", name="smt_auto_request_message"),
        sa.CheckConstraint("purpose IN ('PREPARE','COMPLETION')", name="smt_auto_request_purpose"),
    )
    op.create_table(
        "smt_auto_supply_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id"), nullable=False
        ),
        sa.Column(
            "plan_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id"), nullable=False
        ),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("rack_id", sa.String(100), nullable=False),
        sa.Column("rack_face", sa.String(50)),
        sa.Column("rack_array_index", sa.Integer(), nullable=False),
        sa.Column("face_array_index", sa.Integer()),
        sa.Column("withdrawn_by_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id")),
        sa.Column("withdrawn_at_ms", sa.BigInteger()),
        sa.Column("supply_change_id", sa.String(200), nullable=False),
        sa.UniqueConstraint(
            "task_id",
            "plan_revision",
            "rack_id",
            "rack_face",
            name="smt_auto_supply_member_identity",
        ),
    )
    op.create_table(
        "smt_auto_bin_work",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("arrival_id", sa.String(200), nullable=False),
        sa.Column("device_code", sa.String(100), nullable=False),
        sa.Column("raw_bin_code", sa.String(100), nullable=False),
        sa.Column("bin_code", sa.String(100)),
        sa.Column("scanned_at", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id")),
        sa.Column("business_type", sa.String(20)),
        sa.Column("association", sa.String(20), nullable=False),
        sa.Column("production_hold_reason", sa.String(100)),
        sa.Column("exception_basis", sa.JSON()),
        sa.Column("terminal_basis", sa.JSON()),
        sa.Column("terminal_at_ms", sa.BigInteger()),
        sa.Column("release_decided_at_ms", sa.BigInteger()),
        sa.Column("release_command_code", sa.String(100)),
        sa.Column("release_result_ref", sa.String(200)),
        sa.Column("release_result_snapshot", sa.JSON()),
        sa.Column("release_consumed_at_ms", sa.BigInteger()),
        sa.Column("pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_check_at_ms", sa.BigInteger()),
        sa.UniqueConstraint("work_line_id", "arrival_id", name="smt_auto_bin_work_arrival"),
        sa.CheckConstraint(
            "association IN ('TASK','NO_TASK','EXCEPTION')", name="smt_auto_bin_work_association"
        ),
    )

    op.create_table(
        "smt_auto_inbound_source_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id"), nullable=False
        ),
        sa.Column(
            "plan_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id"), nullable=False
        ),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("rack_id", sa.String(100), nullable=False),
        sa.Column("rack_array_index", sa.Integer(), nullable=False),
        sa.Column("withdrawn_by_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id")),
        sa.Column("withdrawn_at_ms", sa.BigInteger()),
        sa.UniqueConstraint(
            "task_id", "plan_revision", "rack_id", name="smt_auto_inbound_source_member_identity"
        ),
    )
    op.create_table(
        "smt_auto_outbound_source_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.String(100), sa.ForeignKey("smt_auto_task.task_id"), nullable=False
        ),
        sa.Column(
            "plan_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id"), nullable=False
        ),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False),
        sa.Column("rack_id", sa.String(100), nullable=False),
        sa.Column("rack_face", sa.String(50), nullable=False),
        sa.Column("rack_array_index", sa.Integer(), nullable=False),
        sa.Column("withdrawn_by_input_id", sa.Integer(), sa.ForeignKey("smt_auto_task_input.id")),
        sa.Column("withdrawn_at_ms", sa.BigInteger()),
        sa.UniqueConstraint(
            "task_id", "rack_id", "rack_face", name="smt_auto_outbound_source_member_identity"
        ),
    )


def downgrade():
    for name in (
        "smt_auto_outbound_source_member",
        "smt_auto_inbound_source_member",
        "smt_auto_bin_work",
        "smt_auto_supply_member",
        "smt_auto_task_request",
        "smt_auto_task_input",
    ):
        op.drop_table(name)
    op.drop_index("smt_auto_task_execution_slot", table_name="smt_auto_task")
    op.drop_index("smt_auto_task_queue", table_name="smt_auto_task")
    op.drop_table("smt_auto_task")
