"""Keep original rack recovery evidence in the inbound terminal summary."""

import sqlalchemy as sa
from alembic import op

revision = "smtautoretention"
down_revision = "smtautobarcode"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("smt_auto_task", sa.Column("rack_retention_basis", sa.JSON(), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE smt_auto_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM smt_auto_task WHERE rack_retention_basis IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError("AUTO archived rack recovery evidence exists; downgrade refused")
    with op.batch_alter_table("smt_auto_task") as batch:
        batch.drop_column("rack_retention_basis")
