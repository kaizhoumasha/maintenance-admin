"""Retain original first work-plan and face terminal evidence."""

import sqlalchemy as sa
from alembic import op

revision = "smtautofirstplan"
down_revision = "smtautorackfacts"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("smt_auto_outbound_source_member") as batch:
        batch.add_column(sa.Column("first_work_plan_round_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("terminal_basis", sa.JSON(none_as_null=True), nullable=True))
        batch.add_column(sa.Column("terminal_at_ms", sa.BigInteger(), nullable=True))
        batch.create_foreign_key(
            "auto_source_first_round_fk",
            "smt_auto_outbound_decision_round",
            ["first_work_plan_round_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "auto_source_first_round_unique", ["first_work_plan_round_id"]
        )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM smt_auto_outbound_source_member "
                "WHERE first_work_plan_round_id IS NOT NULL "
                "OR terminal_basis IS NOT NULL OR terminal_at_ms IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("AUTO first work-plan evidence exists; downgrade refused")
    with op.batch_alter_table("smt_auto_outbound_source_member") as batch:
        batch.drop_constraint("auto_source_first_round_unique", type_="unique")
        batch.drop_constraint("auto_source_first_round_fk", type_="foreignkey")
        batch.drop_column("terminal_at_ms")
        batch.drop_column("terminal_basis")
        batch.drop_column("first_work_plan_round_id")
