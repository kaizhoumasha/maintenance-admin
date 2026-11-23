"""Retain explicit rack mismatch positions without changing outcome settlement."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "f0b220261031"
down_revision = "f0b120261030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transport_task", sa.Column("position_observation", sa.JSON(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM transport_task WHERE position_observation IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("transport_task 存在原位置事实，禁止删除 position_observation")
    with op.batch_alter_table("transport_task") as batch:
        batch.drop_column("position_observation")
