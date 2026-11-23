"""Allow a coarse sorting job to retain and complete its original NG move."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "coarsejobstage2"
down_revision = "coarsejobstage1"
branch_labels = None
depends_on = None

JOB = "coarse_sorting_job"
ACTION = "coarse_sorting_action"
JOB_STATES = (
    "'AWAITING_VALIDATION', 'AWAITING_MOVE', 'AWAITING_ALLOCATION', "
    "'AWAITING_PUT', 'AWAITING_REPORT', 'AWAITING_NG', 'COMPLETED'"
)
ECS_KINDS = "'MOVE_FORWARD', 'PUT_TO_BIN'"
WMS_KINDS = "'VALIDATE_BIND', 'ALLOCATE_STORAGE', 'REPORT_COMPLETION'"


def _action_checks(schema, ecs_kinds):
    with op.batch_alter_table(ACTION, schema=schema) as batch:
        kind = op.f(f"{ACTION}_kind_check")
        protocol = op.f(f"{ACTION}_protocol_check")
        batch.drop_constraint(kind, type_="check")
        batch.drop_constraint(protocol, type_="check")
        batch.create_check_constraint(kind, f"kind IN ({ecs_kinds}, {WMS_KINDS})")
        batch.create_check_constraint(
            protocol,
            f"((kind IN ({WMS_KINDS})) "
            "AND operation IS NOT NULL AND operation_id IS NOT NULL AND message_id IS NOT NULL "
            "AND command_owner IS NULL AND call_key IS NULL "
            "AND command_code IS NULL AND device_code IS NULL) OR "
            f"((kind IN ({ecs_kinds})) "
            "AND operation IS NULL AND operation_id IS NULL AND message_id IS NULL "
            "AND command_owner IS NOT NULL AND call_key IS NOT NULL "
            "AND command_code IS NOT NULL AND device_code IS NOT NULL "
            "AND resource_snapshot IS NOT NULL)",
        )


def _job_checks(schema, *, ng):
    with op.batch_alter_table(JOB, schema=schema) as batch:
        state = op.f(f"{JOB}_state_check")
        completion = op.f(f"{JOB}_completion_check")
        reason = op.f(f"{JOB}_ng_check")
        batch.drop_constraint(state, type_="check")
        batch.drop_constraint(completion, type_="check")
        if ng:
            batch.add_column(sa.Column("ng_reason_code", sa.String(), nullable=True))
            batch.create_check_constraint(state, f"state IN ({JOB_STATES}, 'NG_COMPLETED')")
            batch.create_check_constraint(
                completion, "(state IN ('COMPLETED', 'NG_COMPLETED')) = (completed_at IS NOT NULL)"
            )
            batch.create_check_constraint(
                reason,
                "(ng_reason_code IS NULL OR binding_id IS NULL) "
                "AND (state <> 'NG_COMPLETED' "
                "OR (ng_reason_code IS NOT NULL AND current_action_id IS NOT NULL))",
            )
        else:
            batch.drop_constraint(reason, type_="check")
            batch.drop_column("ng_reason_code")
            batch.create_check_constraint(state, f"state IN ({JOB_STATES})")
            batch.create_check_constraint(
                completion, "(state = 'COMPLETED') = (completed_at IS NOT NULL)"
            )


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    _job_checks(schema, ng=True)
    _action_checks(schema, f"{ECS_KINDS}, 'MOVE_TO_NG'")


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(
            sa.text(f"LOCK TABLE {schema}.{JOB}, {schema}.{ACTION} IN ACCESS EXCLUSIVE MODE")
        )
    job = sa.table(JOB, sa.column("ng_reason_code"), schema=schema)
    action = sa.table(ACTION, sa.column("kind"), schema=schema)
    if (
        bind.scalar(
            sa.select(job.c.ng_reason_code).where(job.c.ng_reason_code.is_not(None)).limit(1)
        )
        is not None
        or bind.scalar(sa.select(action.c.kind).where(action.c.kind == "MOVE_TO_NG").limit(1))
        is not None
    ):
        raise CommandError("coarse NG evidence exists; refusing destructive downgrade")
    _job_checks(schema, ng=False)
    _action_checks(schema, ECS_KINDS)
