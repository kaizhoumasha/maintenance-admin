"""Track complete dispatch evidence for scoped static messages."""

import sqlalchemy as sa
from alembic import op

revision = "scoped_dispatch_evidence_v1"
down_revision = "http_projection_pending_v1"
branch_labels = None
depends_on = None


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.add_column(
        "reliable_message", sa.Column("dispatch_attempt_count", sa.Integer()), schema=schema
    )


def downgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_column("reliable_message", "dispatch_attempt_count", schema=schema)
