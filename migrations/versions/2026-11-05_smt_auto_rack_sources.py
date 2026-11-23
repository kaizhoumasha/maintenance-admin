"""Permit the three original AUTO rack responsibilities in the shared source constraint."""

import sqlalchemy as sa
from alembic import op

revision = "smtautosources"
down_revision = "smtautoretention"
branch_labels = None
depends_on = None

TABLE = "rack_dispatch_job"
CHECK = "rack_dispatch_job_source_kind_check"
LEGACY = "'MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINARY','ROLLER_DRAIN'"
AUTO = "'AUTO_INBOUND_SOURCE','AUTO_OUTBOUND_RETURN','AUTO_OUTBOUND_TARGET'"


def _replace(allowed):
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), f"source_kind IN ({allowed})")


def upgrade():
    _replace(f"{LEGACY},{AUTO}")


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE rack_dispatch_job IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text(f"SELECT 1 FROM rack_dispatch_job WHERE source_kind IN ({AUTO}) LIMIT 1")
    ).first():
        raise RuntimeError("AUTO rack source evidence exists; downgrade refused")
    _replace(LEGACY)
