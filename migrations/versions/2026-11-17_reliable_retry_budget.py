"""Keep the retry budget boundary when resuming an original outbound message."""

import sqlalchemy as sa
from alembic import op

revision = "reliable_retry_budget_v1"
down_revision = "scoped_unsent_transport_v1"
branch_labels = None
depends_on = None


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.add_column(
        "reliable_delivery_attempt",
        sa.Column(
            "counts_toward_retry_budget", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        schema=schema,
    )


def downgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_column("reliable_delivery_attempt", "counts_toward_retry_budget", schema=schema)
