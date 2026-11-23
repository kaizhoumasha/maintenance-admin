"""Store the original ECS device on callback receipts for equality lookup."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "ea8b20261029"
down_revision = "d7cf20261029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("callback_receipt", sa.Column("device_code", sa.Text(), nullable=True))
    op.create_index(
        "callback_receipt_device_idx",
        "callback_receipt",
        ["device_code"],
        postgresql_using="hash",
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE callback_receipt IN ACCESS EXCLUSIVE MODE"))
    if connection.execute(
        sa.text("SELECT 1 FROM callback_receipt WHERE device_code IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("callback_receipt 存在设备码证据，禁止删除 device_code")
    op.drop_index("callback_receipt_device_idx", table_name="callback_receipt")
    with op.batch_alter_table("callback_receipt") as batch:
        batch.drop_column("device_code")
