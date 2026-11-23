"""Add nullable runtime slots ahead of the authority reconciliation."""

import sqlalchemy as sa
from alembic import op

revision = "http_exchange_runtime_expand"
down_revision = "http_exchange_request_authority"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    for name in (
        "pending_result_receipt_id",
        "applied_result_receipt_id",
        "applied_trigger_receipt_id",
    ):
        op.add_column("transport_task", sa.Column(name, sa.BigInteger()), schema=schema)
    op.add_column(
        "transport_task", sa.Column("pending_result_source_unknown", sa.Boolean()), schema=schema
    )
    for name, kind in (
        ("terminal_response_wire", sa.LargeBinary()),
        ("last_response_wire", sa.LargeBinary()),
        ("terminal_response_at", sa.DateTime(timezone=True)),
        ("protocol", sa.Text()),
        ("superseded_by_message_id", sa.Integer()),
        ("lease_not_started", sa.Boolean()),
    ):
        op.add_column("reliable_message", sa.Column(name, kind), schema=schema)
    op.add_column(
        "reliable_message",
        sa.Column("replacement_blocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=schema,
    )
    for name, kind in (
        ("request_wire", sa.LargeBinary()),
        ("response_wire", sa.LargeBinary()),
        ("response_http_status", sa.SmallInteger()),
        ("response_verdict", sa.Text()),
    ):
        op.add_column("reliable_delivery_attempt", sa.Column(name, kind), schema=schema)


def downgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    for table, names in (
        (
            "transport_task",
            (
                "pending_result_receipt_id",
                "applied_result_receipt_id",
                "applied_trigger_receipt_id",
                "pending_result_source_unknown",
            ),
        ),
        (
            "reliable_message",
            (
                "terminal_response_wire",
                "last_response_wire",
                "terminal_response_at",
                "protocol",
                "superseded_by_message_id",
                "lease_not_started",
                "replacement_blocked",
            ),
        ),
        (
            "reliable_delivery_attempt",
            ("request_wire", "response_wire", "response_http_status", "response_verdict"),
        ),
    ):
        for name in names:
            op.drop_column(table, name, schema=schema)
