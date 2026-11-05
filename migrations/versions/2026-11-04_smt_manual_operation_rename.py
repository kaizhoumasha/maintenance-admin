"""Rename the manual rack completion operation without losing existing inputs."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "smtmanualoprename"
down_revision = "rollerfour"
branch_labels = None
depends_on = None

OLD_OPERATION = "outbound.manual_rack.direct_pick_completed@v1"
NEW_OPERATION = "manual_rack.direct_pick_completed@v1"
SHAPE_CONSTRAINT = "smt_manual_input_smt_manual_input_shape_check"

SHAPE_CHECK = (
    "(operation IN "
    "('sorting.task.issued@v1','sorting.task.queue_changed@v1','sorting.task.plan"
    "_delta@v1','sorting.task.cancel@v1') AND state IN "
    "('APPLIED','DEFERRED','REJECTED') AND rack_id IS NULL AND rack_face IS NULL "
    "AND bin_code IS NULL AND arrival_id IS NULL AND completed_at IS NULL AND "
    "completion_result IS NULL AND original_operation_id IS NULL AND ((operation "
    "= 'sorting.task.plan_delta@v1' AND plan_revision IS NOT NULL) OR (operation "
    "!= 'sorting.task.plan_delta@v1' AND plan_revision IS NULL))) OR (operation "
    "= 'manual_bin.work_completed@v1' AND receipt_id IS NOT NULL AND bin_code IS "
    "NOT NULL AND completed_at IS NOT NULL AND completion_result IS NOT NULL AND "
    "plan_revision IS NULL AND rack_id IS NULL AND rack_face IS NULL AND state "
    "IN ('PENDING','APPLIED','DUPLICATE','REVIEW')) OR (operation IN "
    "('manual_rack.direct_pick_completed@v1','outbound.return_rack.task_"
    "completed@v1') AND receipt_id IS NOT NULL AND rack_id IS NOT NULL AND "
    "plan_revision IS NOT NULL AND completed_at IS NOT NULL AND bin_code IS NULL "
    "AND arrival_id IS NULL AND completion_result IS NULL AND "
    "original_operation_id IS NULL AND reason IS NULL AND state IN "
    "('PENDING','APPLIED') AND ((operation = "
    "'manual_rack.direct_pick_completed@v1' AND rack_face IS NOT NULL) "
    "OR (operation = 'outbound.return_rack.task_completed@v1' AND rack_face IS "
    "NULL)))"
)


def _schema(bind):
    return "enterprise_backend" if bind.dialect.name == "postgresql" else None


def _input_table(schema):
    metadata = sa.MetaData(schema=schema)
    return sa.Table(
        "smt_manual_input",
        metadata,
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
    )


def _drop_constraint(schema):
    with op.batch_alter_table("smt_manual_input", schema=schema) as batch:
        batch.drop_constraint(op.f(SHAPE_CONSTRAINT), type_="check")


def _create_constraint(schema, expression):
    with op.batch_alter_table("smt_manual_input", schema=schema) as batch:
        batch.create_check_constraint(op.f(SHAPE_CONSTRAINT), expression)


def upgrade() -> None:
    bind = op.get_bind()
    schema = _schema(bind)
    table = _input_table(schema)
    old = table.alias("old_input")
    new = table.alias("new_input")
    collision = (
        sa.select(sa.literal(1))
        .select_from(old)
        .join(
            new,
            sa.and_(
                old.c.operation == OLD_OPERATION,
                new.c.operation == NEW_OPERATION,
                old.c.operation_id == new.c.operation_id,
            ),
        )
    )
    if bind.execute(collision.limit(1)).first() is not None:
        raise CommandError("smt_manual_input contains conflicting renamed operation identities")

    _drop_constraint(schema)
    bind.execute(
        table.update().where(table.c.operation == OLD_OPERATION).values(operation=NEW_OPERATION)
    )
    _create_constraint(schema, SHAPE_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    schema = _schema(bind)
    table = _input_table(schema)
    _drop_constraint(schema)
    bind.execute(
        table.update().where(table.c.operation == NEW_OPERATION).values(operation=OLD_OPERATION)
    )
    _create_constraint(schema, SHAPE_CHECK.replace(NEW_OPERATION, OLD_OPERATION))
