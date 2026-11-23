"""Delivery-budget stall state, uncertain-delivery marker and not-received counter."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a019c20261022"
down_revision = "a019b20261021"
branch_labels = None
depends_on = None

# 基础迁移里写的是 name="status";命名约定(ck_%(table_name)s_%(constraint_name)s_check)
# 使数据库中的实际名称为下面这个,已在 PostgreSQL 与 SQLite 上核对。
STATUS_CHECK = sa.schema.conv("reliable_message_status_check")
SCHEDULE_CHECK = sa.schema.conv("reliable_message_next_attempt_check")


def upgrade() -> None:
    with op.batch_alter_table("reliable_message", recreate="auto") as batch:
        batch.add_column(
            sa.Column("not_received_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("delivery_uncertain", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.alter_column(
            "next_attempt_at", existing_type=sa.DateTime(timezone=True), nullable=True
        )
        batch.drop_constraint(STATUS_CHECK, type_="check")
        batch.create_check_constraint(
            STATUS_CHECK, "status IN ('PENDING','SENDING','STALLED','DONE','FAILED','EXPIRED')"
        )
        batch.create_check_constraint(
            SCHEDULE_CHECK, "status = 'STALLED' OR next_attempt_at IS NOT NULL"
        )
    # 旧表只保存最后一次结果,无法证明历史上没有出现过未知;保守视为未知,保持原身份自动消歧。
    op.execute(
        sa.text(
            "UPDATE reliable_message SET delivery_uncertain = :unknown "
            "WHERE status IN ('PENDING','SENDING')"
        ).bindparams(unknown=True)
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE reliable_message IN ACCESS EXCLUSIVE MODE"))
    protected = connection.execute(
        sa.text(
            "SELECT 1 FROM reliable_message WHERE status = 'STALLED' OR (status IN "
            "('PENDING','SENDING') AND (delivery_uncertain OR not_received_count > 0)) LIMIT 1"
        )
    ).first()
    if protected:
        raise CommandError("reliable_message 含挂起消息或未知送达事实，禁止破坏性降级")
    with op.batch_alter_table("reliable_message", recreate="auto") as batch:
        batch.drop_constraint(SCHEDULE_CHECK, type_="check")
        batch.drop_constraint(STATUS_CHECK, type_="check")
        batch.create_check_constraint(
            STATUS_CHECK, "status IN ('PENDING','SENDING','DONE','FAILED','EXPIRED')"
        )
        batch.alter_column(
            "next_attempt_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
        batch.drop_column("delivery_uncertain")
        batch.drop_column("not_received_count")
