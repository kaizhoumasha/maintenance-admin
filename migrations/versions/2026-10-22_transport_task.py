"""Transport call identity and immutable input digest."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a019d20261022"
down_revision = "a019c20261022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transport_task",
        sa.Column("id", sa.Integer(), primary_key=True),
        *(
            sa.Column(name, sa.Text(), nullable=False)
            for name in (
                "client_request_id",
                "transport_task_id",
                "operation_id",
                "workline_id",
            )
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("station_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_request_id"),
        sa.UniqueConstraint("transport_task_id"),
        sa.UniqueConstraint("operation_id"),
        sa.CheckConstraint(
            "kind IN ('RACK_MOVE','RACK_ROTATE','BIN_MOVE','BIN_EXCHANGE')", name="kind"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(sa.text("SELECT 1 FROM transport_task LIMIT 1")).first():
        raise CommandError("transport_task 非空，禁止删除 Transport 幂等映射")
    op.drop_table("transport_task")
