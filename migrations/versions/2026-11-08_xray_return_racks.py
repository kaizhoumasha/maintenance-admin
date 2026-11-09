"""Associate X-Ray outlet decisions with original shared rack preparation."""

import sqlalchemy as sa
from alembic import op

from app.database import SCHEMA

revision = "xray_return_racks"
down_revision = "xray_reels"
branch_labels = None
depends_on = None

ORIGINS = (
    "'MANUAL_RETURN','MANUAL_TRANSFER','ROLLER_ORDINARY','ROLLER_DRAIN',"
    "'AUTO_INBOUND_SOURCE','AUTO_OUTBOUND_RETURN','AUTO_OUTBOUND_TARGET',"
    "'COARSE_INCOMING','COARSE_WMS_PRESENT'"
)


def _sources(schema, origins):
    name = op.f("rack_dispatch_job_source_kind_check")
    with op.batch_alter_table("rack_dispatch_job", schema=schema) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, f"source_kind IN ({origins})")


def upgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    _sources(schema, ORIGINS + ",'XRAY_RETURN'")
    op.add_column("xray_reel_operations", sa.Column("rack_basis", sa.JSON()), schema=schema)
    op.add_column(
        "xray_reel_operations", sa.Column("rack_preparation_id", sa.Integer()), schema=schema
    )
    operation_id = "xray_reel_operations.operation_id"
    if schema:
        operation_id = f"{schema}.{operation_id}"
    op.create_table(
        "xray_rack_preparations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("point", sa.String(100), nullable=False),
        sa.Column(
            "operation_id", sa.String(36), sa.ForeignKey(operation_id), nullable=False, unique=True
        ),
        sa.Column("old_job_id", sa.String(36)),
        sa.Column("incoming_job_id", sa.String(36)),
        sa.Column("action_id", sa.String(36)),
        sa.Column("pending", sa.Boolean(), nullable=False),
        schema=schema,
    )
    op.create_index(
        "xray_preparation_point",
        "xray_rack_preparations",
        ["work_line_id", "point", "id"],
        schema=schema,
    )


def downgrade():
    bind = op.get_bind()
    schema = SCHEMA if bind.dialect.name == "postgresql" else None
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM xray_rack_preparations) OR "
            "EXISTS (SELECT 1 FROM rack_dispatch_job WHERE source_kind='XRAY_RETURN')"
        )
    ):
        raise RuntimeError("X-Ray rack evidence exists; downgrade refused")
    op.drop_table("xray_rack_preparations", schema=schema)
    with op.batch_alter_table("xray_reel_operations", schema=schema) as batch:
        batch.drop_column("rack_preparation_id")
        batch.drop_column("rack_basis")
    _sources(schema, ORIGINS)
