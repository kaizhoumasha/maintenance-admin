"""Freeze the explicitly configured first-plan wait at original prepare acceptance."""

import sqlalchemy as sa
from alembic import op

revision = "smtautofirstdeadline"
down_revision = "smtautosourcearrival"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("smt_auto_task", sa.Column("first_plan_wait_ms", sa.BigInteger(), nullable=True))
    op.add_column(
        "smt_auto_task", sa.Column("first_plan_deadline_ms", sa.BigInteger(), nullable=True)
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM smt_auto_task WHERE first_plan_wait_ms IS NOT NULL "
                "OR first_plan_deadline_ms IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("AUTO original first-plan deadline exists; downgrade refused")
    op.drop_column("smt_auto_task", "first_plan_deadline_ms")
    op.drop_column("smt_auto_task", "first_plan_wait_ms")
