"""Local cycle grouping key on transport_task."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a023a20261024"
down_revision = "a022a20261023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("transport_task", recreate="auto") as batch:
        batch.add_column(sa.Column("cycle_id", sa.Text()))
        batch.create_index("transport_task_cycle_idx", ["cycle_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM transport_task WHERE cycle_id IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("transport_task 已保存生命周期分组键，禁止破坏性降级")
    with op.batch_alter_table("transport_task", recreate="auto") as batch:
        batch.drop_index("transport_task_cycle_idx")
        batch.drop_column("cycle_id")
