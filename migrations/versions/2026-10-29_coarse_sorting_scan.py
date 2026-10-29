"""Persist coarse scan facts independently of callback receipt retention."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "c5ca20261029"
down_revision = "a6d464b20ab7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.create_table(
        "coarse_sorting_scan",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("device_code", sa.String(), nullable=False),
        sa.Column("device_role", sa.String(), nullable=False),
        sa.Column("device_version", sa.Integer(), nullable=False),
        sa.Column("work_line_version", sa.Integer(), nullable=False),
        sa.Column("binding_revision", sa.String(), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("wait_reason", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="coarse_sorting_scan_pkey"),
        sa.UniqueConstraint("event_digest", name="coarse_sorting_scan_event_digest_key"),
        schema=schema,
    )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(
            sa.text("LOCK TABLE enterprise_backend.coarse_sorting_scan IN ACCESS EXCLUSIVE MODE")
        )
    table = sa.table("coarse_sorting_scan", sa.column("id"), schema=schema)
    if bind.execute(sa.select(table.c.id).limit(1)).first():
        raise CommandError("coarse scan records exist; refusing destructive downgrade")
    op.drop_table("coarse_sorting_scan", schema=schema)
