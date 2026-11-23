"""Generic callback receipts and best-effort snapshots."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a020a20261022"
down_revision = "a019d20261022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "callback_receipt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation", sa.String(256), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("raw_digest", sa.String(64), nullable=False),
        sa.Column("effective_digest", sa.String(64)),
        sa.Column("compare_mode", sa.String(9), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("resp_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation", "operation_id"),
        sa.CheckConstraint(
            "(compare_mode = 'RAW' AND effective_digest IS NULL) OR "
            "(compare_mode = 'EFFECTIVE' AND effective_digest IS NOT NULL)",
            name="compare_mode",
        ),
    )
    op.create_table(
        "callback_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body_b64", sa.Text(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON()),
        sa.Column("outcome", sa.String(32), nullable=False),
    )
    op.create_index("callback_snapshot_retention_idx", "callback_snapshot", ["received_at", "id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE callback_receipt IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(sa.text("SELECT 1 FROM callback_receipt LIMIT 1")).first():
        raise CommandError("callback_receipt 非空，禁止删除入站幂等收据")
    op.drop_index("callback_snapshot_retention_idx", table_name="callback_snapshot")
    op.drop_table("callback_snapshot")
    op.drop_table("callback_receipt")
