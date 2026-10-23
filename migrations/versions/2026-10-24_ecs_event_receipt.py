"""ECS event receipt: widen callback_receipt.operation_id, add callback_snapshot.source."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a024a20261024"
down_revision = "a023a20261024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 不强制 VARCHAR 长度,无需改列
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "callback_receipt",
            "operation_id",
            existing_type=sa.String(36),
            type_=sa.String(64),
            existing_nullable=False,
        )
    op.add_column("callback_snapshot", sa.Column("source", sa.String(16)))


def downgrade() -> None:
    connection = op.get_bind()
    postgresql = connection.dialect.name == "postgresql"
    if postgresql:
        connection.execute(sa.text("LOCK TABLE callback_receipt IN ACCESS EXCLUSIVE MODE"))
        connection.execute(sa.text("LOCK TABLE callback_snapshot IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM callback_receipt WHERE length(operation_id) > 36 LIMIT 1")
    ).first():
        raise CommandError("callback_receipt 存在超过 36 位的 operation_id，禁止收窄")
    if connection.execute(
        sa.text("SELECT 1 FROM callback_snapshot WHERE source IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("callback_snapshot 存在非空 source，禁止删除该列")
    op.drop_column("callback_snapshot", "source")
    if postgresql:
        op.alter_column(
            "callback_receipt",
            "operation_id",
            existing_type=sa.String(64),
            type_=sa.String(36),
            existing_nullable=False,
        )
