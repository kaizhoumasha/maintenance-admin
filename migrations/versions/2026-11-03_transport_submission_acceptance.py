"""Keep original Transport submission acceptance independently of the outbox."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "racksubmitack"
down_revision = "fiveracklimit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transport_task",
        sa.Column("submission_acceptance", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM transport_task WHERE submission_acceptance IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("transport_task 存在原接纳证据，禁止删除 submission_acceptance")
    with op.batch_alter_table("transport_task") as batch:
        batch.drop_column("submission_acceptance")
