"""Add logical workline bindings without changing existing device configuration."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "c31f20261027"
down_revision = "11e090516bf1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "enterprise_backend"
    for name in ("device_bindings", "position_bindings"):
        op.add_column(
            "work_lines",
            sa.Column(name, sa.JSON(), server_default="{}", nullable=False),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(sa.text("LOCK TABLE enterprise_backend.work_lines IN ACCESS EXCLUSIVE MODE"))
    table = sa.table(
        "work_lines",
        sa.column("device_bindings", sa.JSON()),
        sa.column("position_bindings", sa.JSON()),
        schema=schema,
    )
    if any(devices or positions for devices, positions in bind.execute(sa.select(table))):
        raise CommandError("workline bindings exist; refusing destructive downgrade")
    for name in ("position_bindings", "device_bindings"):
        op.drop_column("work_lines", name, schema=schema)
