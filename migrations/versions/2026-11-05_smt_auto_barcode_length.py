"""Preserve the confirmed 1000 code point raw barcode in both AUTO directions."""

import sqlalchemy as sa
from alembic import op

revision = "smtautobarcode"
down_revision = "smtautofirstdeadline"
branch_labels = None
depends_on = None

TABLES = ("smt_auto_inbound_plate", "smt_auto_outbound_plate")


def upgrade():
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "barcode",
                existing_type=sa.String(200),
                type_=sa.String(1000),
                existing_nullable=True,
            )


def downgrade():
    for table in TABLES:
        if (
            op.get_bind()
            .execute(sa.text(f"SELECT 1 FROM {table} WHERE length(barcode) > 200 LIMIT 1"))
            .first()
        ):
            raise RuntimeError("AUTO raw barcode exceeds 200 code points; downgrade refused")
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "barcode",
                existing_type=sa.String(1000),
                type_=sa.String(200),
                existing_nullable=True,
            )
