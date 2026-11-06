"Add shared coarse rack preparation without changing existing action identities."

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "coarserackstage3"
down_revision = "coarsejobstage2"
branch_labels = None
depends_on = None

ACTION_CHECKS = {
    "kind": (
        "kind IN ('VALIDATE_BIND', 'MOVE_FORWARD', 'ALLOCATE_STORAGE', 'P"
        "UT_TO_BIN', 'REPORT_COMPLETION', 'MOVE_TO_NG', 'DEPARTURE_DECIDE"
        "', 'MOVE_RACK_OUT', 'MOVE_RACK_IN')"
    ),
    "round": (
        "round_no >= 1 AND (kind IN ('ALLOCATE_STORAGE', 'DEPARTURE_DECIDE') OR round_no = 1)"
    ),
    "generation": (
        "(kind = 'ALLOCATE_STORAGE') = (rack_generation_no IS NOT NULL) A"
        "ND (rack_generation_no IS NULL OR rack_generation_no >= 0)"
    ),
    "protocol": (
        "((kind IN ('VALIDATE_BIND', 'ALLOCATE_STORAGE', 'REPORT_COMPLETI"
        "ON', 'DEPARTURE_DECIDE')) AND operation IS NOT NULL AND operatio"
        "n_id IS NOT NULL AND message_id IS NOT NULL AND command_owner IS"
        " NULL AND call_key IS NULL AND command_code IS NULL AND device_c"
        "ode IS NULL) OR ((kind IN ('MOVE_FORWARD', 'PUT_TO_BIN', 'MOVE_T"
        "O_NG')) AND operation IS NULL AND operation_id IS NULL AND messa"
        "ge_id IS NULL AND command_owner IS NOT NULL AND call_key IS NOT "
        "NULL AND command_code IS NOT NULL AND device_code IS NOT NULL AN"
        "D resource_snapshot IS NOT NULL) OR (kind IN ('MOVE_RACK_IN', 'M"
        "OVE_RACK_OUT') AND operation IS NULL AND operation_id IS NULL AN"
        "D message_id IS NULL AND command_owner IS NULL AND call_key IS N"
        "ULL AND command_code IS NULL AND device_code IS NULL AND resourc"
        "e_snapshot IS NOT NULL AND transport_task_id IS NOT NULL AND cli"
        "ent_request_id IS NOT NULL AND cycle_id IS NOT NULL AND transpor"
        "t_caller IS NOT NULL)"
    ),
    "owner": "(job_id IS NULL) <> (preparation_id IS NULL)",
    "owner_kind": (
        "(job_id IS NULL) = (kind IN ('DEPARTURE_DECIDE','MOVE_RACK_IN','MOVE_RACK_OUT'))"
    ),
}


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    prefix = f"{schema}." if schema else ""
    op.create_table(
        "coarse_sorting_rack_preparation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("rack_position_code", sa.String(), nullable=False),
        sa.Column("rack_binding_version", sa.Integer(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("old_rack_id", sa.String(), nullable=True),
        sa.Column("incoming_rack_id", sa.String(), nullable=True),
        sa.Column("incoming_target_face", sa.String(length=10), nullable=True),
        sa.Column("source_allocation_action_id", sa.String(length=36), nullable=False),
        sa.Column("predecessor_action_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("wait_reason", sa.String(), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_action_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            (
                "(mode='REPLENISH' AND old_rack_id IS NULL AND incoming_rack_id I"
                "S NOT NULL AND incoming_target_face IS NOT NULL) OR (mode='REPLA"
                "CE' AND old_rack_id IS NOT NULL AND incoming_rack_id IS NOT NULL"
                " AND incoming_rack_id <> old_rack_id AND incoming_target_face IS"
                " NOT NULL) OR (mode='DEPART_ONLY' AND old_rack_id IS NOT NULL AN"
                "D incoming_rack_id IS NULL AND incoming_target_face IS NULL)"
            ),
            name=op.f("coarse_sorting_rack_preparation_plan_check"),
        ),
        sa.CheckConstraint(
            "(state='READY') = (ready_at IS NOT NULL)",
            name=op.f("coarse_sorting_rack_preparation_ready_check"),
        ),
        sa.CheckConstraint(
            ("state IN ('AWAITING_DEPARTURE','MOVING_OUT','MOVING_IN','READY','DEPART_ACCEPTED')"),
            name=op.f("coarse_sorting_rack_preparation_state_check"),
        ),
        sa.CheckConstraint(
            "generation_no >= 1", name=op.f("coarse_sorting_rack_preparation_generation_check")
        ),
        sa.ForeignKeyConstraint(
            ["current_action_id"],
            [prefix + "coarse_sorting_action.id"],
            name=op.f("coarse_sorting_rack_preparation_current_action_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["source_allocation_action_id"],
            [prefix + "coarse_sorting_action.id"],
            name=op.f("coarse_sorting_rack_preparation_source_allocation_action_id_fkey"),
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("coarse_sorting_rack_preparation_pkey")),
        sa.UniqueConstraint(
            "source_allocation_action_id",
            name=op.f("coarse_sorting_rack_preparation_source_allocation_action_id_key"),
        ),
        sa.UniqueConstraint(
            "work_line_id",
            "rack_position_code",
            "generation_no",
            name="coarse_sorting_preparation_generation_key",
        ),
        schema=schema,
    )
    op.create_index(
        "coarse_sorting_preparation_active_key",
        "coarse_sorting_rack_preparation",
        ["work_line_id", "rack_position_code"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("state NOT IN ('READY','DEPART_ACCEPTED')"),
        postgresql_where=sa.text("state NOT IN ('READY','DEPART_ACCEPTED')"),
    )
    if schema:
        for name in ("current_action_id", "source_allocation_action_id"):
            op.create_foreign_key(
                op.f(f"coarse_sorting_rack_preparation_{name}_fkey"),
                "coarse_sorting_rack_preparation",
                "coarse_sorting_action",
                [name],
                ["id"],
                source_schema=schema,
                referent_schema=schema,
                ondelete="RESTRICT",
                use_alter=True,
            )
    with op.batch_alter_table("coarse_sorting_action", schema=schema) as batch:
        for name in ("preparation_id", "input_preparation_id", "result_preparation_id"):
            batch.add_column(sa.Column(name, sa.String(36), nullable=True))
            batch.create_foreign_key(
                op.f(f"coarse_sorting_action_{name}_fkey"),
                "coarse_sorting_rack_preparation",
                [name],
                ["id"],
                referent_schema=schema,
                ondelete="RESTRICT",
                use_alter=True,
            )
        for name in ("rack_job_id", "rack_action_id", "transport_task_id"):
            batch.add_column(sa.Column(name, sa.String(), nullable=True))
        for name in ("client_request_id", "cycle_id"):
            batch.add_column(sa.Column(name, sa.String(36), nullable=True))
        batch.add_column(sa.Column("transport_caller", sa.JSON(none_as_null=True), nullable=True))
        batch.alter_column("job_id", existing_type=sa.String(), nullable=True)
        for name in ("transport_task_id", "client_request_id"):
            batch.create_unique_constraint(op.f(f"coarse_sorting_action_{name}_key"), [name])
        batch.create_unique_constraint(
            "coarse_sorting_action_preparation_kind_round_key",
            ["preparation_id", "kind", "round_no"],
        )
        for name, expression in ACTION_CHECKS.items():
            if name not in ("owner", "owner_kind"):
                batch.drop_constraint(op.f(f"coarse_sorting_action_{name}_check"), type_="check")
            batch.create_check_constraint(op.f(f"coarse_sorting_action_{name}_check"), expression)
    with op.batch_alter_table("coarse_sorting_job", schema=schema) as batch:
        batch.add_column(sa.Column("rack_preparation_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            op.f("coarse_sorting_job_rack_preparation_id_fkey"),
            "coarse_sorting_rack_preparation",
            ["rack_preparation_id"],
            ["id"],
            referent_schema=schema,
            ondelete="RESTRICT",
            use_alter=True,
        )
        batch.drop_constraint(op.f("coarse_sorting_job_state_check"), type_="check")
        batch.create_check_constraint(
            op.f("coarse_sorting_job_state_check"),
            (
                "state IN ('AWAITING_VALIDATION', 'AWAITING_MOVE', 'AWAITING_ALLO"
                "CATION', 'AWAITING_RACK', 'AWAITING_PUT', 'AWAITING_REPORT', 'AW"
                "AITING_NG', 'COMPLETED', 'NG_COMPLETED')"
            ),
        )
    with op.batch_alter_table("rack_dispatch_job", schema=schema) as batch:
        batch.drop_constraint(op.f("rack_dispatch_job_source_kind_check"), type_="check")
        batch.create_check_constraint(
            op.f("rack_dispatch_job_source_kind_check"),
            (
                "source_kind IN ('MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINA"
                "RY','ROLLER_DRAIN','AUTO_INBOUND_SOURCE','AUTO_OUTBOUND_RETURN',"
                "'AUTO_OUTBOUND_TARGET','COARSE_INCOMING','COARSE_WMS_PRESENT')"
            ),
        )


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    prefix = f"{schema}." if schema else ""
    if schema:
        bind.execute(
            sa.text(
                f"LOCK TABLE {prefix}coarse_sorting_rack_preparation, "
                f"{prefix}coarse_sorting_action, {prefix}coarse_sorting_job, "
                f"{prefix}rack_dispatch_job IN ACCESS EXCLUSIVE MODE"
            )
        )
    if bind.scalar(
        sa.text(f"SELECT 1 FROM {prefix}coarse_sorting_rack_preparation LIMIT 1")
    ) or bind.scalar(
        sa.text(
            f"SELECT 1 FROM {prefix}rack_dispatch_job WHERE source_kind "
            "IN ('COARSE_INCOMING','COARSE_WMS_PRESENT') LIMIT 1"
        )
    ):
        raise CommandError("coarse rack original evidence exists; refusing destructive downgrade")
    with op.batch_alter_table("coarse_sorting_job", schema=schema) as batch:
        batch.drop_constraint(
            op.f("coarse_sorting_job_rack_preparation_id_fkey"), type_="foreignkey"
        )
        batch.drop_column("rack_preparation_id")
        batch.drop_constraint(op.f("coarse_sorting_job_state_check"), type_="check")
        batch.create_check_constraint(
            op.f("coarse_sorting_job_state_check"),
            (
                "state IN ('AWAITING_VALIDATION', 'AWAITING_MOVE', 'AWAITING_ALLO"
                "CATION', 'AWAITING_PUT', 'AWAITING_REPORT', 'AWAITING_NG', 'COMP"
                "LETED', 'NG_COMPLETED')"
            ),
        )
    with op.batch_alter_table("coarse_sorting_action", schema=schema) as batch:
        for name in ("preparation_id", "input_preparation_id", "result_preparation_id"):
            batch.drop_constraint(op.f(f"coarse_sorting_action_{name}_fkey"), type_="foreignkey")
        for name in ("transport_task_id", "client_request_id"):
            batch.drop_constraint(op.f(f"coarse_sorting_action_{name}_key"), type_="unique")
        batch.drop_constraint("coarse_sorting_action_preparation_kind_round_key", type_="unique")
        for name in ACTION_CHECKS:
            batch.drop_constraint(op.f(f"coarse_sorting_action_{name}_check"), type_="check")
        for name, expression in {
            "kind": (
                "kind IN ('VALIDATE_BIND', 'MOVE_FORWARD', 'ALLOCATE_STORAGE', 'P"
                "UT_TO_BIN', 'REPORT_COMPLETION', 'MOVE_TO_NG')"
            ),
            "round": "round_no >= 1 AND (kind = 'ALLOCATE_STORAGE' OR round_no = 1)",
            "generation": (
                "(kind = 'ALLOCATE_STORAGE') = (rack_generation_no IS NOT NULL) A"
                "ND (rack_generation_no IS NULL OR rack_generation_no >= 0)"
            ),
            "protocol": (
                "((kind IN ('VALIDATE_BIND', 'ALLOCATE_STORAGE', 'REPORT_COMPLETI"
                "ON')) AND operation IS NOT NULL AND operation_id IS NOT NULL AND"
                " message_id IS NOT NULL AND command_owner IS NULL AND call_key I"
                "S NULL AND command_code IS NULL AND device_code IS NULL) OR ((ki"
                "nd IN ('MOVE_FORWARD', 'PUT_TO_BIN', 'MOVE_TO_NG')) AND operatio"
                "n IS NULL AND operation_id IS NULL AND message_id IS NULL AND co"
                "mmand_owner IS NOT NULL AND call_key IS NOT NULL AND command_cod"
                "e IS NOT NULL AND device_code IS NOT NULL AND resource_snapshot "
                "IS NOT NULL)"
            ),
        }.items():
            batch.create_check_constraint(op.f(f"coarse_sorting_action_{name}_check"), expression)
        for name in (
            "preparation_id",
            "input_preparation_id",
            "result_preparation_id",
            "rack_job_id",
            "rack_action_id",
            "transport_task_id",
            "client_request_id",
            "cycle_id",
            "transport_caller",
        ):
            batch.drop_column(name)
        batch.alter_column("job_id", existing_type=sa.String(), nullable=False)
    op.drop_table("coarse_sorting_rack_preparation", schema=schema)
    with op.batch_alter_table("rack_dispatch_job", schema=schema) as batch:
        batch.drop_constraint(op.f("rack_dispatch_job_source_kind_check"), type_="check")
        batch.create_check_constraint(
            op.f("rack_dispatch_job_source_kind_check"),
            (
                "source_kind IN ('MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINA"
                "RY','ROLLER_DRAIN','AUTO_INBOUND_SOURCE','AUTO_OUTBOUND_RETURN',"
                "'AUTO_OUTBOUND_TARGET')"
            ),
        )
