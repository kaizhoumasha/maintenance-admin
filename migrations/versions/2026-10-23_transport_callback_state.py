"""Transport callback state on transport_task, receipt diagnostics, frozen backfill."""

import json

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "a021a20261023"
down_revision = "a020a20261022"
branch_labels = None
depends_on = None

TASK_STATE = (
    "outcome_status",
    "member_positions",
    "applied_outcome",
    "outcome_digests",
    "pending_outcome",
)


def upgrade() -> None:
    with op.batch_alter_table("transport_task", recreate="auto") as batch:
        batch.add_column(sa.Column("frozen", sa.JSON()))
        batch.add_column(sa.Column("member_positions", sa.JSON()))
        batch.add_column(sa.Column("outcome_status", sa.String(16)))
        batch.add_column(sa.Column("applied_outcome", sa.JSON()))
        batch.add_column(sa.Column("applied_outcome_revision", sa.BigInteger()))
        batch.add_column(sa.Column("outcome_digests", sa.JSON()))
        batch.add_column(sa.Column("pending_outcome", sa.JSON()))
    with op.batch_alter_table("callback_receipt", recreate="auto") as batch:
        batch.add_column(sa.Column("diagnostic", sa.JSON()))
    _backfill_frozen(op.get_bind())


def _backfill_frozen(connection) -> None:
    """冻结的请求字节还在 reliable_message.request_raw 里:取出 data(去掉 transport_task_id)回填。
    消息已被保留期清理的任务保持为空,回调到达时按「冻结信息缺失」处理。"""
    tasks = sa.table(
        "transport_task",
        sa.column("id"),
        sa.column("transport_task_id"),
        sa.column("operation_id"),
        sa.column("frozen", sa.JSON()),
    )
    messages = sa.table(
        "reliable_message",
        sa.column("business_key"),
        sa.column("operation_id"),
        sa.column("request_raw"),
    )
    for row_id, task_id, operation_id in connection.execute(
        sa.select(tasks.c.id, tasks.c.transport_task_id, tasks.c.operation_id)
    ).all():
        # 只取该任务自己的提交消息(同 business_key 下可能还有别的 operation)
        raw = connection.execute(
            sa.select(messages.c.request_raw).where(
                messages.c.business_key == task_id, messages.c.operation_id == operation_id
            )
        ).scalar()
        if raw is None:
            continue
        try:
            data = json.loads(raw)["data"]
            data.pop("transport_task_id", None)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        connection.execute(tasks.update().where(tasks.c.id == row_id).values(frozen=data))


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE transport_task IN ACCESS EXCLUSIVE MODE"))
        connection.execute(sa.text("LOCK TABLE callback_receipt IN ACCESS EXCLUSIVE MODE"))
    used = " OR ".join(f"{column} IS NOT NULL" for column in TASK_STATE)
    if connection.execute(sa.text(f"SELECT 1 FROM transport_task WHERE {used} LIMIT 1")).first():
        raise CommandError("transport_task 已保存回调应用结果，禁止破坏性降级")
    if connection.execute(
        sa.text("SELECT 1 FROM callback_receipt WHERE diagnostic IS NOT NULL LIMIT 1")
    ).first():
        raise CommandError("callback_receipt 含诊断记录，禁止破坏性降级")
    with op.batch_alter_table("callback_receipt", recreate="auto") as batch:
        batch.drop_column("diagnostic")
    with op.batch_alter_table("transport_task", recreate="auto") as batch:
        for column in (
            "pending_outcome",
            "outcome_digests",
            "applied_outcome_revision",
            "applied_outcome",
            "outcome_status",
            "member_positions",
            "frozen",
        ):
            batch.drop_column(column)
