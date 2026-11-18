"""Record an operator-confirmed AUTO supply bypass."""

import sqlalchemy as sa
from alembic import op

revision = "smt_auto_manual_bypass_v1"
down_revision = "reliable_dispatch_fence_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "smt_auto_supply_member",
        sa.Column("manual_bypass_evidence", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade():
    op.drop_column("smt_auto_supply_member", "manual_bypass_evidence")
