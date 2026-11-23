"""Add AUTO original associations and direction facts without rewriting S1 rows."""

import sqlalchemy as sa
from alembic import op

revision = "smtautorackfacts"
down_revision = "smtautofoundation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("smt_auto_task_input", sa.Column("target_job_id", sa.String(100), nullable=True))
    op.add_column(
        "smt_auto_task_input",
        sa.Column("target_preparation_action_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "smt_auto_task_input", sa.Column("source_rack_job_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "smt_auto_task_input",
        sa.Column("source_preparation_action_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "smt_auto_inbound_source_member", sa.Column("rack_job_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "smt_auto_inbound_source_member",
        sa.Column("preparation_action_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "smt_auto_outbound_source_member", sa.Column("rack_job_id", sa.String(100), nullable=True)
    )
    op.add_column(
        "smt_auto_outbound_source_member",
        sa.Column("preparation_action_id", sa.String(100), nullable=True),
    )
    op.create_table(
        "smt_auto_inbound_plate",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=100), nullable=False, primary_key=False),
        sa.Column("bin_work_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("source_rack_job_id", sa.String(length=100), nullable=True, primary_key=False),
        sa.Column(
            "source_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("target_rack_job_id", sa.String(length=100), nullable=True, primary_key=False),
        sa.Column(
            "target_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("grant_kind", sa.String(length=20), nullable=False, primary_key=False),
        sa.Column("grant_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("grant_parent_plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("grant_snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_locator", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_params", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_arrival_snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("pick_call_key", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("pick_command_code", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("pick_result_ref", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("barcode", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_call_key", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_command_code", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_result_ref", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("report_operation_id", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column(
            "pick_result_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "empty_observation", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("target_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column(
            "next_source_action", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "put_result_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("report_data", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column("report_response", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column(
            "source_closed_basis", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "physical_closure_basis", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("pick_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("scanned_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("put_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("report_message_id", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("report_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column(
            "direct_obligations_closed_at_ms", sa.BigInteger(), nullable=True, primary_key=False
        ),
        sa.Column("next_check_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("final_decision_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("pending", sa.Boolean(), nullable=False, primary_key=False),
        sa.UniqueConstraint("pick_call_key", name="smt_auto_inbound_plate_pick_call_key_key"),
        sa.UniqueConstraint("put_command_code", name="smt_auto_inbound_plate_put_command_code_key"),
        sa.UniqueConstraint("id", "task_id", name="inbound_plate_task"),
        sa.CheckConstraint(
            "grant_kind IN ('READY','NEXT','RETRY')",
            name=op.f("smt_auto_inbound_plate_inbound_plate_grant_check"),
        ),
        sa.UniqueConstraint(
            "report_operation_id", name="smt_auto_inbound_plate_report_operation_id_key"
        ),
        sa.CheckConstraint(
            "bin_work_id IS NOT NULL",
            name=op.f("smt_auto_inbound_plate_inbound_plate_subject_check"),
        ),
        sa.UniqueConstraint(
            "grant_parent_plate_id", name="smt_auto_inbound_plate_grant_parent_plate_id_key"
        ),
        sa.UniqueConstraint("put_call_key", name="smt_auto_inbound_plate_put_call_key_key"),
        sa.UniqueConstraint("grant_round_id", name="smt_auto_inbound_plate_grant_round_id_key"),
        sa.CheckConstraint(
            "(grant_kind = 'NEXT' AND grant_parent_plate_id IS NOT NULL AND "
            "grant_round_id IS NULL) OR (grant_kind IN ('READY','RETRY') AND "
            "grant_round_id IS NOT NULL AND grant_parent_plate_id IS NULL)",
            name=op.f("smt_auto_inbound_plate_inbound_plate_grant_origin_check"),
        ),
    )
    op.create_table(
        "smt_auto_inbound_decision_round",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=100), nullable=False, primary_key=False),
        sa.Column("purpose", sa.String(length=30), nullable=False, primary_key=False),
        sa.Column("bin_work_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column(
            "source_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("trigger_plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("stage_key", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("round_no", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("previous_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("operation", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("operation_id", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False, primary_key=False),
        sa.Column("request_data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column(
            "source_rack_bindings", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "response_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("response_recorded_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("unresolved", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("wait_recorded_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("retry_after_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("next_eligible_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_preparation_action_id IS NULL AND "
            "plate_id IS NULL AND purpose = 'WORK_PLAN') OR (bin_work_id IS NULL AND "
            "source_preparation_action_id IS NOT NULL AND plate_id IS NULL AND purpose "
            "= 'WORK_PLAN') OR (bin_work_id IS NULL AND source_preparation_action_id IS "
            "NULL AND plate_id IS NOT NULL AND purpose IN "
            "('MATERIAL_DECIDE','EMPTY_DECIDE'))",
            name=op.f("smt_auto_inbound_decision_round_inbound_round_subject_check"),
        ),
        sa.UniqueConstraint("task_id", "stage_key", "round_no", name="inbound_round_stage"),
        sa.UniqueConstraint("operation", "operation_id", name="inbound_round_operation"),
        sa.UniqueConstraint("message_id", name="smt_auto_inbound_decision_round_message_id_key"),
        sa.CheckConstraint(
            "purpose IN ('WORK_PLAN','MATERIAL_DECIDE','EMPTY_DECIDE') AND round_no >= 1",
            name=op.f("smt_auto_inbound_decision_round_inbound_round_purpose_check"),
        ),
        sa.UniqueConstraint(
            "previous_round_id", name="smt_auto_inbound_decision_round_previous_round_id_key"
        ),
        sa.UniqueConstraint("id", "task_id", name="inbound_round_task"),
    )
    op.create_index(
        "inbound_round_unresolved",
        "smt_auto_inbound_decision_round",
        ["task_id", "stage_key"],
        unique=True,
        postgresql_where=sa.text("unresolved"),
        sqlite_where=sa.text("unresolved = 1"),
    )
    op.create_table(
        "smt_auto_outbound_plate",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=100), nullable=False, primary_key=False),
        sa.Column("bin_work_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("source_rack_job_id", sa.String(length=100), nullable=True, primary_key=False),
        sa.Column(
            "source_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("target_rack_job_id", sa.String(length=100), nullable=True, primary_key=False),
        sa.Column(
            "target_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("source_member_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("grant_kind", sa.String(length=20), nullable=False, primary_key=False),
        sa.Column("grant_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("grant_parent_plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("grant_snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_locator", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_params", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("source_arrival_snapshot", sa.JSON(), nullable=False, primary_key=False),
        sa.Column("pick_call_key", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("pick_command_code", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("pick_result_ref", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("barcode", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_call_key", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_command_code", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("put_result_ref", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column("report_operation_id", sa.String(length=200), nullable=True, primary_key=False),
        sa.Column(
            "pick_result_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "empty_observation", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("target_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column(
            "next_source_action", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "put_result_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("report_data", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column("report_response", sa.JSON(none_as_null=True), nullable=True, primary_key=False),
        sa.Column(
            "source_closed_basis", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "physical_closure_basis", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("pick_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("scanned_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("put_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("report_message_id", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("report_consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column(
            "direct_obligations_closed_at_ms", sa.BigInteger(), nullable=True, primary_key=False
        ),
        sa.Column("next_check_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("final_decision_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("pending", sa.Boolean(), nullable=False, primary_key=False),
        sa.UniqueConstraint("put_call_key", name="smt_auto_outbound_plate_put_call_key_key"),
        sa.CheckConstraint(
            "grant_kind IN ('READY','NEXT','RETRY')",
            name=op.f("smt_auto_outbound_plate_outbound_plate_grant_check"),
        ),
        sa.UniqueConstraint("grant_round_id", name="smt_auto_outbound_plate_grant_round_id_key"),
        sa.UniqueConstraint("id", "task_id", name="outbound_plate_task"),
        sa.UniqueConstraint("pick_call_key", name="smt_auto_outbound_plate_pick_call_key_key"),
        sa.UniqueConstraint(
            "put_command_code", name="smt_auto_outbound_plate_put_command_code_key"
        ),
        sa.CheckConstraint(
            "(grant_kind = 'NEXT' AND grant_parent_plate_id IS NOT NULL AND "
            "grant_round_id IS NULL) OR (grant_kind IN ('READY','RETRY') AND "
            "grant_round_id IS NOT NULL AND grant_parent_plate_id IS NULL)",
            name=op.f("smt_auto_outbound_plate_outbound_plate_grant_origin_check"),
        ),
        sa.CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_member_id IS NULL) OR (bin_work_id IS "
            "NULL AND source_member_id IS NOT NULL)",
            name=op.f("smt_auto_outbound_plate_outbound_plate_subject_check"),
        ),
        sa.UniqueConstraint(
            "report_operation_id", name="smt_auto_outbound_plate_report_operation_id_key"
        ),
        sa.UniqueConstraint(
            "grant_parent_plate_id", name="smt_auto_outbound_plate_grant_parent_plate_id_key"
        ),
    )
    op.create_table(
        "smt_auto_outbound_decision_round",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=100), nullable=False, primary_key=False),
        sa.Column("purpose", sa.String(length=30), nullable=False, primary_key=False),
        sa.Column("bin_work_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column(
            "source_preparation_action_id", sa.String(length=100), nullable=True, primary_key=False
        ),
        sa.Column("plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("trigger_plate_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("stage_key", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("round_no", sa.Integer(), nullable=False, primary_key=False),
        sa.Column("previous_round_id", sa.Integer(), nullable=True, primary_key=False),
        sa.Column("operation", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("operation_id", sa.String(length=200), nullable=False, primary_key=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False, primary_key=False),
        sa.Column("request_data", sa.JSON(), nullable=False, primary_key=False),
        sa.Column(
            "source_rack_bindings", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column(
            "response_snapshot", sa.JSON(none_as_null=True), nullable=True, primary_key=False
        ),
        sa.Column("response_recorded_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("consumed_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("unresolved", sa.Boolean(), nullable=False, primary_key=False),
        sa.Column("wait_recorded_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("retry_after_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.Column("next_eligible_at_ms", sa.BigInteger(), nullable=True, primary_key=False),
        sa.UniqueConstraint(
            "previous_round_id", name="smt_auto_outbound_decision_round_previous_round_id_key"
        ),
        sa.UniqueConstraint("id", "task_id", name="outbound_round_task"),
        sa.CheckConstraint(
            "(bin_work_id IS NOT NULL AND source_preparation_action_id IS NULL AND "
            "plate_id IS NULL AND purpose = 'WORK_PLAN') OR (bin_work_id IS NULL AND "
            "source_preparation_action_id IS NOT NULL AND plate_id IS NULL AND purpose "
            "= 'WORK_PLAN') OR (bin_work_id IS NULL AND source_preparation_action_id IS "
            "NULL AND plate_id IS NOT NULL AND purpose IN "
            "('MATERIAL_DECIDE','EMPTY_DECIDE'))",
            name=op.f("smt_auto_outbound_decision_round_outbound_round_subject_check"),
        ),
        sa.UniqueConstraint("task_id", "stage_key", "round_no", name="outbound_round_stage"),
        sa.UniqueConstraint("operation", "operation_id", name="outbound_round_operation"),
        sa.UniqueConstraint("message_id", name="smt_auto_outbound_decision_round_message_id_key"),
        sa.CheckConstraint(
            "purpose IN ('WORK_PLAN','MATERIAL_DECIDE','EMPTY_DECIDE') AND round_no >= 1",
            name=op.f("smt_auto_outbound_decision_round_outbound_round_purpose_check"),
        ),
    )
    op.create_index(
        "outbound_round_unresolved",
        "smt_auto_outbound_decision_round",
        ["task_id", "stage_key"],
        unique=True,
        postgresql_where=sa.text("unresolved"),
        sqlite_where=sa.text("unresolved = 1"),
    )
    with op.batch_alter_table("smt_auto_inbound_plate") as batch:
        batch.create_foreign_key(
            "smt_auto_inbound_plate_bin_work_id_fkey", "smt_auto_bin_work", ["bin_work_id"], ["id"]
        )
        batch.create_foreign_key(
            "inbound_plate_parent_fk",
            "smt_auto_inbound_plate",
            ["grant_parent_plate_id", "task_id"],
            ["id", "task_id"],
        )
        batch.create_foreign_key(
            "inbound_plate_round_fk",
            "smt_auto_inbound_decision_round",
            ["grant_round_id", "task_id"],
            ["id", "task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_inbound_plate_final_decision_round_id_fkey",
            "smt_auto_inbound_decision_round",
            ["final_decision_round_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "smt_auto_inbound_plate_task_id_fkey", "smt_auto_task", ["task_id"], ["task_id"]
        )
    with op.batch_alter_table("smt_auto_inbound_decision_round") as batch:
        batch.create_foreign_key(
            "smt_auto_inbound_decision_round_plate_id_fkey",
            "smt_auto_inbound_plate",
            ["plate_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "smt_auto_inbound_decision_round_task_id_fkey",
            "smt_auto_task",
            ["task_id"],
            ["task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_inbound_decision_round_trigger_plate_id_fkey",
            "smt_auto_inbound_plate",
            ["trigger_plate_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "inbound_round_previous_fk",
            "smt_auto_inbound_decision_round",
            ["previous_round_id", "task_id"],
            ["id", "task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_inbound_decision_round_bin_work_id_fkey",
            "smt_auto_bin_work",
            ["bin_work_id"],
            ["id"],
        )
    with op.batch_alter_table("smt_auto_outbound_plate") as batch:
        batch.create_foreign_key(
            "smt_auto_outbound_plate_source_member_id_fkey",
            "smt_auto_outbound_source_member",
            ["source_member_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "outbound_plate_parent_fk",
            "smt_auto_outbound_plate",
            ["grant_parent_plate_id", "task_id"],
            ["id", "task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_plate_task_id_fkey", "smt_auto_task", ["task_id"], ["task_id"]
        )
        batch.create_foreign_key(
            "outbound_plate_round_fk",
            "smt_auto_outbound_decision_round",
            ["grant_round_id", "task_id"],
            ["id", "task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_plate_final_decision_round_id_fkey",
            "smt_auto_outbound_decision_round",
            ["final_decision_round_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_plate_bin_work_id_fkey", "smt_auto_bin_work", ["bin_work_id"], ["id"]
        )
    with op.batch_alter_table("smt_auto_outbound_decision_round") as batch:
        batch.create_foreign_key(
            "smt_auto_outbound_decision_round_plate_id_fkey",
            "smt_auto_outbound_plate",
            ["plate_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_decision_round_task_id_fkey",
            "smt_auto_task",
            ["task_id"],
            ["task_id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_decision_round_trigger_plate_id_fkey",
            "smt_auto_outbound_plate",
            ["trigger_plate_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "smt_auto_outbound_decision_round_bin_work_id_fkey",
            "smt_auto_bin_work",
            ["bin_work_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "outbound_round_previous_fk",
            "smt_auto_outbound_decision_round",
            ["previous_round_id", "task_id"],
            ["id", "task_id"],
        )


def downgrade():
    bind = op.get_bind()
    for name in (
        "smt_auto_inbound_plate",
        "smt_auto_outbound_plate",
        "smt_auto_inbound_decision_round",
        "smt_auto_outbound_decision_round",
    ):
        if bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first():
            raise RuntimeError("AUTO rack facts contain original evidence; downgrade refused")
    for name, columns in (
        (
            "smt_auto_task_input",
            (
                "target_job_id",
                "target_preparation_action_id",
                "source_rack_job_id",
                "source_preparation_action_id",
            ),
        ),
        ("smt_auto_inbound_source_member", ("rack_job_id", "preparation_action_id")),
        ("smt_auto_outbound_source_member", ("rack_job_id", "preparation_action_id")),
    ):
        condition = " OR ".join(f"{column} IS NOT NULL" for column in columns)
        if bind.execute(sa.text(f"SELECT 1 FROM {name} WHERE {condition} LIMIT 1")).first():
            raise RuntimeError(
                "AUTO rack associations contain original evidence; downgrade refused"
            )
    with op.batch_alter_table("smt_auto_outbound_decision_round") as batch:
        batch.drop_constraint("smt_auto_outbound_decision_round_plate_id_fkey", type_="foreignkey")
        batch.drop_constraint("smt_auto_outbound_decision_round_task_id_fkey", type_="foreignkey")
        batch.drop_constraint(
            "smt_auto_outbound_decision_round_trigger_plate_id_fkey", type_="foreignkey"
        )
        batch.drop_constraint(
            "smt_auto_outbound_decision_round_bin_work_id_fkey", type_="foreignkey"
        )
        batch.drop_constraint("outbound_round_previous_fk", type_="foreignkey")
    with op.batch_alter_table("smt_auto_outbound_plate") as batch:
        batch.drop_constraint("smt_auto_outbound_plate_source_member_id_fkey", type_="foreignkey")
        batch.drop_constraint("outbound_plate_parent_fk", type_="foreignkey")
        batch.drop_constraint("smt_auto_outbound_plate_task_id_fkey", type_="foreignkey")
        batch.drop_constraint("outbound_plate_round_fk", type_="foreignkey")
        batch.drop_constraint(
            "smt_auto_outbound_plate_final_decision_round_id_fkey", type_="foreignkey"
        )
        batch.drop_constraint("smt_auto_outbound_plate_bin_work_id_fkey", type_="foreignkey")
    with op.batch_alter_table("smt_auto_inbound_decision_round") as batch:
        batch.drop_constraint("smt_auto_inbound_decision_round_plate_id_fkey", type_="foreignkey")
        batch.drop_constraint("smt_auto_inbound_decision_round_task_id_fkey", type_="foreignkey")
        batch.drop_constraint(
            "smt_auto_inbound_decision_round_trigger_plate_id_fkey", type_="foreignkey"
        )
        batch.drop_constraint("inbound_round_previous_fk", type_="foreignkey")
        batch.drop_constraint(
            "smt_auto_inbound_decision_round_bin_work_id_fkey", type_="foreignkey"
        )
    with op.batch_alter_table("smt_auto_inbound_plate") as batch:
        batch.drop_constraint("smt_auto_inbound_plate_bin_work_id_fkey", type_="foreignkey")
        batch.drop_constraint("inbound_plate_parent_fk", type_="foreignkey")
        batch.drop_constraint("inbound_plate_round_fk", type_="foreignkey")
        batch.drop_constraint(
            "smt_auto_inbound_plate_final_decision_round_id_fkey", type_="foreignkey"
        )
        batch.drop_constraint("smt_auto_inbound_plate_task_id_fkey", type_="foreignkey")
    op.drop_table("smt_auto_outbound_decision_round")
    op.drop_table("smt_auto_outbound_plate")
    op.drop_table("smt_auto_inbound_decision_round")
    op.drop_table("smt_auto_inbound_plate")
    op.drop_column("smt_auto_task_input", "source_preparation_action_id")
    op.drop_column("smt_auto_task_input", "source_rack_job_id")
    op.drop_column("smt_auto_task_input", "target_preparation_action_id")
    op.drop_column("smt_auto_task_input", "target_job_id")
    op.drop_column("smt_auto_inbound_source_member", "preparation_action_id")
    op.drop_column("smt_auto_inbound_source_member", "rack_job_id")
    op.drop_column("smt_auto_outbound_source_member", "preparation_action_id")
    op.drop_column("smt_auto_outbound_source_member", "rack_job_id")
