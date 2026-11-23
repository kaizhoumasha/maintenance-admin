"""Index callback_receipt by created_at for retention cleanup."""

from alembic import op

revision = "a022a20261023"
down_revision = "a021a20261023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("callback_receipt_retention_idx", "callback_receipt", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("callback_receipt_retention_idx", table_name="callback_receipt")
