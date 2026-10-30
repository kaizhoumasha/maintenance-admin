"""Retain original business ownership without changing Transport wire identities."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "f0b120261030"
down_revision = "ea8b20261029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transport_task", sa.Column("owner_domain", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM transport_task WHERE owner_domain IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("transport_task 存在原业务归属，禁止删除 owner_domain")
    with op.batch_alter_table("transport_task") as batch:
        batch.drop_column("owner_domain")
