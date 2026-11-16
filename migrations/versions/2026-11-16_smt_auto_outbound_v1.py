"""Use work plans for normal SMT AUTO outbound continuations."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "smt_auto_outbound_v1"
down_revision = "http_exchange_initiator"
branch_labels = None
depends_on = None

PLATE = "smt_auto_outbound_plate"
GRANT_CHECK = f"{PLATE}_outbound_plate_grant_check"
ORIGIN_CHECK = f"{PLATE}_outbound_plate_grant_origin_check"
PARENT_UNIQUE = f"{PLATE}_grant_parent_plate_id_key"
PARENT_FK = "outbound_plate_parent_fk"
FLAGS = (
    ("smt_auto_bin_work", "normal_plan_due"),
    ("smt_auto_outbound_source_member", "normal_plan_due"),
    ("smt_auto_outbound_decision_round", "normal_continuation"),
)


def _locked_schema(bind):
    if bind.dialect.name != "postgresql":
        return None, ""
    schema = "enterprise_backend"
    prefix = f"{schema}."
    tables = ("sorting_task", *(table for table, _ in FLAGS), PLATE)
    bind.exec_driver_sql(
        f"LOCK TABLE {', '.join(prefix + table for table in tables)} "
        "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
    )
    return schema, prefix


def _require_idle(bind, prefix):
    pending = bind.scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {prefix}sorting_task"
            " WHERE task_type='AUTO' AND business_type='outbound'"
            " AND (status NOT IN ('COMPLETED','CANCELLED') OR pending))"
            f" OR EXISTS (SELECT 1 FROM {prefix}smt_auto_bin_work"
            " WHERE business_type='outbound' AND pending)"
            f" OR EXISTS (SELECT 1 FROM {prefix}smt_auto_outbound_source_member"
            " WHERE arrived_at_ms IS NOT NULL AND terminal_at_ms IS NULL"
            " AND withdrawn_at_ms IS NULL)"
            f" OR EXISTS (SELECT 1 FROM {prefix}{PLATE}"
            " WHERE pending OR (report_message_id IS NOT NULL AND report_consumed_at_ms IS NULL))"
            f" OR EXISTS (SELECT 1 FROM {prefix}smt_auto_outbound_decision_round WHERE unresolved)"
        )
    )
    if pending:
        raise CommandError("SMT AUTO outbound work is still pending; reconcile before cutover")


def upgrade():
    bind = op.get_bind()
    schema, prefix = _locked_schema(bind)
    _require_idle(bind, prefix)
    if bind.scalar(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {prefix}{PLATE} WHERE grant_kind='NEXT')")
    ):
        raise CommandError("SMT AUTO legacy NEXT grants remain; clean settled outbound history")
    if bind.scalar(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {prefix}smt_auto_bin_work AS work"
            " WHERE work.business_type='outbound' AND work.association='TASK'"
            " AND NOT EXISTS ("
            f"SELECT 1 FROM {prefix}smt_auto_outbound_decision_round AS decision"
            " WHERE work.terminal_basis ->> 'kind' = 'NO_WORK'"
            " AND CAST(decision.id AS TEXT) = CAST(work.terminal_basis ->> 'round_id' AS TEXT)"
            " AND decision.operation_id = work.terminal_basis ->> 'operation_id'"
            " AND decision.task_id=work.task_id AND decision.bin_work_id=work.id"
            " AND decision.purpose='WORK_PLAN' AND decision.operation='outbound.bin.work_plan@v1'"
            " AND NOT decision.unresolved AND decision.consumed_at_ms=work.terminal_at_ms"
            " AND decision.response_snapshot -> 'data' ->> 'result' = 'NO_WORK'))"
        )
    ):
        raise CommandError(
            "SMT AUTO legacy outbound BIN history lacks NO_WORK evidence; "
            "clean settled outbound history"
        )
    for table, column in FLAGS:
        op.add_column(
            table,
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
            schema=schema,
        )
    with op.batch_alter_table(PLATE, schema=schema) as batch:
        batch.drop_constraint(op.f(GRANT_CHECK), type_="check")
        batch.drop_constraint(op.f(ORIGIN_CHECK), type_="check")
        batch.drop_constraint(PARENT_FK, type_="foreignkey")
        batch.drop_constraint(PARENT_UNIQUE, type_="unique")
        batch.drop_column("grant_parent_plate_id")
        batch.drop_column("next_source_action")
        batch.create_check_constraint(op.f(GRANT_CHECK), "grant_kind IN ('READY','RETRY')")
        batch.create_check_constraint(op.f(ORIGIN_CHECK), "grant_round_id IS NOT NULL")


def downgrade():
    bind = op.get_bind()
    schema, prefix = _locked_schema(bind)
    _require_idle(bind, prefix)
    for table, column in FLAGS:
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {prefix}{table} WHERE {column})")):
            raise CommandError("SMT AUTO has new continuation evidence; downgrade refused")
    with op.batch_alter_table(PLATE, schema=schema) as batch:
        batch.drop_constraint(op.f(GRANT_CHECK), type_="check")
        batch.drop_constraint(op.f(ORIGIN_CHECK), type_="check")
        batch.add_column(sa.Column("grant_parent_plate_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("next_source_action", sa.JSON(none_as_null=True), nullable=True))
        batch.create_unique_constraint(PARENT_UNIQUE, ["grant_parent_plate_id"])
        batch.create_foreign_key(
            PARENT_FK,
            PLATE,
            ["grant_parent_plate_id", "task_id"],
            ["id", "task_id"],
            referent_schema=schema,
        )
        batch.create_check_constraint(op.f(GRANT_CHECK), "grant_kind IN ('READY','NEXT','RETRY')")
        batch.create_check_constraint(
            op.f(ORIGIN_CHECK),
            "(grant_kind = 'NEXT' AND grant_parent_plate_id IS NOT NULL AND grant_round_id IS NULL)"
            " OR (grant_kind IN ('READY','RETRY') AND grant_round_id IS NOT NULL"
            " AND grant_parent_plate_id IS NULL)",
        )
    for table, column in reversed(FLAGS):
        op.drop_column(table, column, schema=schema)
