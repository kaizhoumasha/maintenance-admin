"""Persist X-Ray reels, physical facts and original WMS operations."""

import sqlalchemy as sa
from alembic import op

from app.database import SCHEMA

revision = "xray_reels"
down_revision = "integrationhistorycontract"
branch_labels = None
depends_on = None


def upgrade():
    schema = SCHEMA if op.get_bind().dialect.name == "postgresql" else None
    reel_id = f"{schema}.xray_reels.id" if schema else "xray_reels.id"
    op.create_table(
        "xray_reels",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(50), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("device_code", sa.String(100), nullable=False),
        sa.Column("reel_id", sa.String(36), nullable=False),
        sa.Column("pending", sa.Boolean(), nullable=False),
        sa.Column("conflict", sa.String(64)),
        sa.Column("wait_reason", sa.String(64)),
        sa.Column("arm_device_code", sa.String(100), unique=True),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("destination", sa.JSON()),
        sa.UniqueConstraint("work_line_id", "device_id", "reel_id", name="xray_reel_identity"),
        schema=schema,
    )
    op.create_index("xray_reel_pending", "xray_reels", ["pending", "id"], schema=schema)
    op.create_index(
        "xray_reel_arm_held", "xray_reels", ["work_line_id", "arm_device_code"], schema=schema
    )
    op.create_table(
        "xray_reel_events",
        sa.Column("receipt_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column(
            "reel_key",
            sa.String(36),
            sa.ForeignKey(reel_id),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("stage", sa.String(10), nullable=False),
        sa.Column("timestamp", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        schema=schema,
    )
    op.create_index(
        "xray_event_device_identity", "xray_reel_events", ["device_id", "event_id"], schema=schema
    )
    op.create_index(
        "xray_event_reel", "xray_reel_events", ["reel_key", "receipt_id"], schema=schema
    )
    op.create_table(
        "xray_reel_operations",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column(
            "reel_key",
            sa.String(36),
            sa.ForeignKey(reel_id),
            nullable=False,
        ),
        sa.Column("stage", sa.String(10), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(36)),
        sa.Column("response", sa.JSON()),
        sa.Column("retry_at", sa.BigInteger()),
        sa.UniqueConstraint("reel_key", "stage", "round", name="xray_operation_round"),
        schema=schema,
    )


def downgrade():
    bind = op.get_bind()
    schema = SCHEMA if bind.dialect.name == "postgresql" else None
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM xray_reels)")):
        raise RuntimeError("X-Ray reel evidence exists; downgrade refused")
    for table in ("xray_reel_operations", "xray_reel_events", "xray_reels"):
        op.drop_table(table, schema=schema)
