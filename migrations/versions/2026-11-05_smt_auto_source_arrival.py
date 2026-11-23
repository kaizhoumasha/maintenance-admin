"""Retain the first reliable original RETURN face confirmation time."""

import sqlalchemy as sa
from alembic import op

revision = "smtautosourcearrival"
down_revision = "smtautofirstplan"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "smt_auto_outbound_source_member",
        sa.Column("arrived_at_ms", sa.BigInteger(), nullable=True),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM smt_auto_outbound_source_member "
                "WHERE arrived_at_ms IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("AUTO original arrival confirmation exists; downgrade refused")
    op.drop_column("smt_auto_outbound_source_member", "arrived_at_ms")
