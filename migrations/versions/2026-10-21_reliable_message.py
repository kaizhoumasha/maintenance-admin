"""Protocol-independent outbound messages."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a019b20261021"
down_revision = "c9f2b7a41d08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reliable_message",
        sa.Column("id", sa.Integer(), primary_key=True),
        *(
            sa.Column(name, sa.Text(), nullable=False)
            for name in ("target", "operation", "operation_id")
        ),
        sa.Column("request_raw", sa.LargeBinary(), nullable=False),
        sa.Column("response_raw", sa.LargeBinary()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fail_reason", sa.String(32)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_http_status", sa.Integer()),
        sa.Column("last_failure_kind", sa.String(64)),
        sa.Column("last_verdict", sa.String(32)),
        sa.Column("last_response_raw", sa.LargeBinary()),
        sa.Column("lease_id", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("business_key", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("trace_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "target", "operation", "operation_id", name="reliable_message_identity_key"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','DONE','FAILED','EXPIRED')", name="status"
        ),
        sqlite_autoincrement=True,
    )
    for label, columns in (
        ("due", ["status", "next_attempt_at"]),
        ("expiry", ["status", "expires_at"]),
        ("lease", ["status", "lease_until"]),
        ("retention", ["status", "updated_at"]),
        ("business_key", ["business_key"]),
    ):
        op.create_index(f"reliable_message_{label}_idx", "reliable_message", columns)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE reliable_message IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(sa.text("SELECT 1 FROM reliable_message LIMIT 1")).first():
        raise CommandError("reliable_message 非空，禁止破坏性降级")
    op.drop_table("reliable_message")
