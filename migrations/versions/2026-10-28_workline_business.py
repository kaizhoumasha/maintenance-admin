"""Add explicit workline business selection; keep legacy lines unselected."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a6d464b20ab7"
down_revision = "c31f20261027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    op.add_column(
        "work_lines", sa.Column("business_code", sa.String(100), nullable=True), schema=schema
    )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(sa.text("LOCK TABLE enterprise_backend.work_lines IN ACCESS EXCLUSIVE MODE"))
    table = sa.table("work_lines", sa.column("business_code", sa.String(100)), schema=schema)
    if bind.execute(
        sa.select(table.c.business_code).where(table.c.business_code.is_not(None)).limit(1)
    ).first():
        raise CommandError("workline business selections exist; refusing destructive downgrade")
    op.drop_column("work_lines", "business_code", schema=schema)
