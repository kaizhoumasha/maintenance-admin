"""Frozen ECS delivery and long-lived command/result evidence."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a025a20261025"
down_revision = "a024a20261024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reliable_message",
        sa.Column("delivery_snapshot", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column("reliable_message", sa.Column("pause_reason", sa.String(64), nullable=True))
    with op.batch_alter_table("reliable_message") as batch:
        batch.drop_constraint(op.f("reliable_message_status_check"), type_="check")
        batch.drop_constraint(op.f("reliable_message_next_attempt_check"), type_="check")
        batch.create_check_constraint(
            op.f("reliable_message_status_check"),
            "status IN ('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED')",
        )
        batch.create_check_constraint(
            op.f("reliable_message_next_attempt_check"),
            "(status IN ('STALLED','HELD') AND next_attempt_at IS NULL) OR (status NOT IN "
            "('STALLED','HELD') AND next_attempt_at IS NOT NULL)",
        )
    op.create_table(
        "ecs_command",
        sa.Column("command_code", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("owner_domain", sa.Text(), primary_key=False, nullable=False),
        sa.Column("call_key", sa.Text(), primary_key=False, nullable=False),
        sa.Column("call_key_digest", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("input_raw", sa.LargeBinary(), primary_key=False, nullable=False),
        sa.Column("device_code", sa.Text(), primary_key=False, nullable=False),
        sa.Column("task_type", sa.Text(), primary_key=False, nullable=False),
        sa.Column("route", sa.JSON(), primary_key=False, nullable=False),
        sa.Column("request_raw", sa.LargeBinary(), primary_key=False, nullable=False),
        sa.Column("target", sa.Text(), primary_key=False, nullable=False),
        sa.Column("delivery_snapshot", sa.JSON(), primary_key=False, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.Column("status", sa.String(length=32), primary_key=False, nullable=False),
        sa.Column("delivery_status", sa.String(length=16), primary_key=False, nullable=False),
        sa.Column(
            "dispatch_started_at", sa.DateTime(timezone=True), primary_key=False, nullable=True
        ),
        sa.Column("ack_raw", sa.LargeBinary(), primary_key=False, nullable=True),
        sa.Column("hold_reason", sa.String(length=64), primary_key=False, nullable=True),
        sa.Column("blocked", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("needs_reconciliation", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("applied_result_id", sa.String(length=64), primary_key=False, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint("owner_domain", "call_key_digest", name="ecs_command_owner_domain_key"),
    )
    op.create_table(
        "ecs_send_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("command_code", sa.String(length=36), primary_key=False, nullable=False),
        sa.Column("lease_id", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("outcome", sa.String(length=32), primary_key=False, nullable=False),
        sa.Column("response_raw", sa.LargeBinary(), primary_key=False, nullable=True),
        sa.Column("http_status", sa.Integer(), primary_key=False, nullable=True),
        sa.Column("late", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint("command_code", "lease_id", name="ecs_send_evidence_command_code_key"),
    )
    op.create_index("ecs_send_evidence_command_code_idx", "ecs_send_evidence", ["command_code"])
    op.create_table(
        "ecs_result_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("command_code", sa.String(length=36), primary_key=False, nullable=False),
        sa.Column("digest", sa.String(length=64), primary_key=False, nullable=False),
        sa.Column("core", sa.JSON(), primary_key=False, nullable=False),
        sa.Column("status_code", sa.Integer(), primary_key=False, nullable=False),
        sa.Column("response", sa.JSON(), primary_key=False, nullable=False),
        sa.Column("applied", sa.Boolean(), primary_key=False, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), primary_key=False, nullable=False),
        sa.UniqueConstraint("command_code", "digest", name="ecs_result_evidence_command_code_key"),
    )
    op.create_index("ecs_result_evidence_command_code_idx", "ecs_result_evidence", ["command_code"])


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        for table in (
            "ecs_command",
            "ecs_send_evidence",
            "ecs_result_evidence",
            "reliable_message",
        ):
            connection.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    for table in ("ecs_command", "ecs_send_evidence", "ecs_result_evidence"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise CommandError("ECS evidence exists; refusing destructive downgrade")
    if connection.execute(
        sa.text(
            "SELECT 1 FROM reliable_message WHERE delivery_snapshot IS NOT NULL OR "
            "status='HELD' OR pause_reason IS NOT NULL LIMIT 1"
        )
    ).first():
        raise CommandError("Snapshot/HELD data exists; refusing destructive downgrade")
    for table in ("ecs_result_evidence", "ecs_send_evidence", "ecs_command"):
        op.drop_table(table)
    with op.batch_alter_table("reliable_message") as batch:
        batch.drop_constraint(op.f("reliable_message_status_check"), type_="check")
        batch.drop_constraint(op.f("reliable_message_next_attempt_check"), type_="check")
        batch.create_check_constraint(
            op.f("reliable_message_status_check"),
            "status IN ('PENDING','SENDING','STALLED','DONE','FAILED','EXPIRED')",
        )
        batch.create_check_constraint(
            op.f("reliable_message_next_attempt_check"),
            "status = 'STALLED' OR next_attempt_at IS NOT NULL",
        )
    op.drop_column("reliable_message", "pause_reason")
    op.drop_column("reliable_message", "delivery_snapshot")
