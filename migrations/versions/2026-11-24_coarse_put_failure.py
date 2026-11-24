"""Persist coarse PUT outcomes separately from WMS report completion."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "coarse_put_failure_v1"
down_revision = "ecs_request_projection_v1"
branch_labels = None
depends_on = None

JOB = "coarse_sorting_job"
PLACEMENT = "coarse_sorting_job_placement_check"
PUT_RESULT = "coarse_sorting_job_put_result_check"
REPORT_STATUS = "coarse_sorting_job_report_status_check"


def upgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    op.add_column(JOB, sa.Column("put_result", sa.String(16)), schema=schema)
    op.add_column(JOB, sa.Column("report_status", sa.String(16)), schema=schema)
    job = sa.table(
        JOB, sa.column("state"), sa.column("put_result"), sa.column("report_status"), schema=schema
    )
    for state, report_status in (("AWAITING_REPORT", "PENDING"), ("COMPLETED", "COMPLETED")):
        bind.execute(
            job.update()
            .where(job.c.state == state)
            .values(put_result="SUCCESS", report_status=report_status)
        )
    with op.batch_alter_table(JOB, schema=schema) as batch:
        batch.drop_constraint(op.f(PLACEMENT), type_="check")
        batch.create_check_constraint(
            op.f(PLACEMENT),
            "put_result IS NULL OR (put_result = 'SUCCESS') = (placed_at_ms IS NOT NULL)",
        )
        batch.create_check_constraint(
            op.f(PUT_RESULT),
            "(state IN ('AWAITING_REPORT', 'COMPLETED')) = (put_result IS NOT NULL) "
            "AND (put_result IS NULL OR put_result IN ('SUCCESS', 'FAILED'))",
        )
        batch.create_check_constraint(
            op.f(REPORT_STATUS),
            "CASE state WHEN 'AWAITING_REPORT' THEN coalesce(report_status = 'PENDING', false) "
            "WHEN 'COMPLETED' THEN coalesce(report_status = 'COMPLETED', false) "
            "ELSE report_status IS NULL END",
        )


def downgrade():
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.exec_driver_sql(f"LOCK TABLE {schema}.{JOB} IN ACCESS EXCLUSIVE MODE")
    job = sa.table(
        JOB,
        sa.column("id"),
        sa.column("put_result"),
        sa.column("report_status"),
        sa.column("current_action_id"),
        schema=schema,
    )
    if bind.scalar(sa.select(job.c.put_result).where(job.c.put_result == "FAILED").limit(1)):
        raise CommandError("coarse failed PUT evidence exists; refusing destructive downgrade")
    action = sa.table(
        "coarse_sorting_action",
        sa.column("id"),
        sa.column("job_id"),
        sa.column("kind"),
        sa.column("operation"),
        schema=schema,
    )
    original_report = sa.select(action.c.id).where(
        action.c.id == job.c.current_action_id,
        action.c.job_id == job.c.id,
        action.c.kind == "REPORT_COMPLETION",
        action.c.operation == "inbound.coarse_sorting.execution_report@v1",
    )
    if (
        bind.scalar(
            sa.select(job.c.id)
            .where(job.c.report_status == "PENDING", ~original_report.exists())
            .limit(1)
        )
        is not None
    ):
        raise CommandError(
            "coarse PUT report has no original action; refusing destructive downgrade"
        )
    with op.batch_alter_table(JOB, schema=schema) as batch:
        for constraint in (PLACEMENT, PUT_RESULT, REPORT_STATUS):
            batch.drop_constraint(op.f(constraint), type_="check")
        batch.create_check_constraint(
            op.f(PLACEMENT),
            "state NOT IN ('AWAITING_REPORT', 'COMPLETED') OR placed_at_ms IS NOT NULL",
        )
        batch.drop_column("report_status")
        batch.drop_column("put_result")
