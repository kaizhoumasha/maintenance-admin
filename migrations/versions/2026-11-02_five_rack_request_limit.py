"""Optional per-workline budget for original FIVE_RACK requests."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "fiveracklimit"
down_revision = "binrollercompact"
branch_labels = None
depends_on = None

OLD_SERVICE_CHECK = (
    "(service_id IS NULL AND source_ref IS NULL AND service_state IS NULL) OR "
    "(service_id IS NOT NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL "
    "AND (purpose != 'SUPPLY' OR source_ref = demand_id) AND service_state IS NOT NULL "
    "AND service_state IN ('PENDING','READY','COMPLETED'))"
)
SERVICE_CHECK = (
    "(service_id IS NULL AND source_ref IS NULL AND service_state IS NULL) OR "
    "(service_id IS NOT NULL AND cycle_id IS NOT NULL AND source_ref IS NOT NULL "
    "AND (purpose != 'SUPPLY' OR source_ref = demand_id) AND service_state IS NOT NULL "
    "AND service_state IN ('PENDING','READY','COMPLETED','WITHDRAWN') "
    "AND (service_state != 'WITHDRAWN' OR "
    "(feed_id IS NULL AND (purpose = 'DRAIN' OR demand_state = 'WITHDRAWN'))))"
)


def service_check(schema, sql) -> None:
    with op.batch_alter_table("bin_roller_face_work", schema=schema) as batch:
        batch.drop_constraint(op.f("bin_roller_face_work_service_check"), type_="check")
        batch.create_check_constraint(op.f("bin_roller_face_work_service_check"), sql)


def upgrade() -> None:
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    # A native column alteration preserves SQLite AUTOINCREMENT and its deleted-ID high water.
    op.add_column(
        "work_lines",
        sa.Column(
            "five_rack_request_limit",
            sa.Integer(),
            sa.CheckConstraint(
                "five_rack_request_limit BETWEEN 1 AND 2147483647 AND "
                "five_rack_request_limit = CAST(five_rack_request_limit AS INTEGER)",
                name=op.f("work_lines_ck_work_lines_five_rack_request_limit_check"),
            ),
            nullable=True,
        ),
        schema=schema,
    )
    service_check(schema, SERVICE_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    if schema:
        bind.execute(sa.text("LOCK TABLE enterprise_backend.work_lines IN ACCESS EXCLUSIVE MODE"))
        bind.execute(
            sa.text("LOCK TABLE enterprise_backend.bin_roller_face_work IN ACCESS EXCLUSIVE MODE")
        )
    table = sa.table("work_lines", sa.column("five_rack_request_limit"), schema=schema)
    if bind.execute(
        sa.select(table.c.five_rack_request_limit)
        .where(table.c.five_rack_request_limit.is_not(None))
        .limit(1)
    ).first():
        raise CommandError("configured rack request limits exist; refusing destructive downgrade")
    services = sa.table(
        "bin_roller_face_work", sa.column("service_state"), sa.column("service_id"), schema=schema
    )
    withdrawn = bind.scalar(
        sa.select(services.c.service_id).where(services.c.service_state == "WITHDRAWN").limit(1)
    )
    if withdrawn is not None:
        raise CommandError(
            f"withdrawn unstarted face services exist: {withdrawn}; refusing lossy downgrade"
        )
    service_check(schema, OLD_SERVICE_CHECK)
    op.drop_column("work_lines", "five_rack_request_limit", schema=schema)
