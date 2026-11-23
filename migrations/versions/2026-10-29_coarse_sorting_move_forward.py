"""Retain original coarse validation and conveyor command evidence."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "d7cf20261029"
down_revision = "c5ca20261029"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("validation_operation_id", sa.String),
    ("validation_message_id", sa.Integer),
    ("validation_response", sa.JSON),
    ("binding_id", sa.String),
    ("conveyor_device_id", sa.Integer),
    ("conveyor_device_code", sa.String),
    ("conveyor_device_version", sa.Integer),
    ("command_code", sa.String),
    ("move_result", sa.JSON),
)


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    for name, kind in _COLUMNS:
        op.add_column("coarse_sorting_scan", sa.Column(name, kind(), nullable=True), schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(
            sa.text("LOCK TABLE enterprise_backend.coarse_sorting_scan IN ACCESS EXCLUSIVE MODE")
        )
    table = sa.table(
        "coarse_sorting_scan", *(sa.column(name) for name, _ in _COLUMNS), schema=schema
    )
    evidence = sa.or_(*(column.is_not(None) for column in table.c))
    if bind.execute(sa.select(sa.literal(1)).select_from(table).where(evidence).limit(1)).first():
        raise CommandError("coarse outbound evidence exists; refusing destructive downgrade")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("coarse_sorting_scan", name, schema=schema)
