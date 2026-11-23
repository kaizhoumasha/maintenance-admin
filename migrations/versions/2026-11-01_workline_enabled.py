"""workline_enabled

Revision ID: a39a1988a81a
Revises: f0b220261031
Create Date: 2026-11-01 11:50:57.765032+00:00
"""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a39a1988a81a"
down_revision = "f0b220261031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.add_column(
        "work_lines",
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=schema,
    )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(sa.text("LOCK TABLE enterprise_backend.work_lines IN ACCESS EXCLUSIVE MODE"))
    table = sa.table("work_lines", sa.column("is_enabled", sa.Boolean()), schema=schema)
    if bind.execute(
        sa.select(table.c.is_enabled).where(table.c.is_enabled.is_(False)).limit(1)
    ).first():
        raise CommandError("disabled worklines exist; refusing destructive downgrade")
    op.drop_column("work_lines", "is_enabled", schema=schema)
