"""Persist X-Ray scan eligibility separately from unclosed business obligations."""

import sqlalchemy as sa
from alembic import op

from app.database import SCHEMA

revision = "xray_pending_due"
down_revision = "xray_return_racks"
branch_labels = None
depends_on = None

INDEXES = (
    ("xray_reels", "xray_reel_due"),
    ("xray_rack_preparations", "xray_preparation_due"),
)


def upgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    for table, index in INDEXES:
        op.add_column(
            table,
            sa.Column("next_check_at_ms", sa.BigInteger(), server_default=sa.text("0")),
            schema=schema,
        )
        op.create_index(index, table, ["pending", "next_check_at_ms", "id"], schema=schema)
    op.drop_index("xray_reel_pending", table_name="xray_reels", schema=schema)


def downgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    for table, index in INDEXES:
        op.drop_index(index, table_name=table, schema=schema)
        with op.batch_alter_table(table, schema=schema) as batch:
            batch.drop_column("next_check_at_ms")
    op.create_index("xray_reel_pending", "xray_reels", ["pending", "id"], schema=schema)
