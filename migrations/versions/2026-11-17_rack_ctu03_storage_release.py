"""Allow point release on the original CTU03 storage rejection."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "rack_ctu03_storage_release"
down_revision = "smt_auto_outbound_v1"
branch_labels = None
depends_on = None

TABLE = "rack_dispatch_job"
CLOSURE = (
    "(state IN ('ACTIVE','RELEASING') AND pending AND closure_reason IS NULL AND "
    "closure_basis IS NULL) OR (NOT pending AND closure_reason IS NOT NULL AND "
    "closure_basis IS NOT NULL AND ((state='COMPLETED' AND (closure_reason='LEGACY_CLOSED' OR "
    "({departure} AND completed_at_ms IS NOT NULL))) OR "
    "(state IN ('WITHDRAWN','NOT_ARRIVED') AND closure_reason=state))) OR "
    "(state='CLOSED_BY_SCOPE' AND closure_reason IS NOT NULL AND closure_reason=state "
    "AND closure_basis IS NOT NULL)"
)


def _schema():
    bind = op.get_bind()
    return None if bind.dialect.name == "sqlite" else bind.dialect.default_schema_name


def _replace_check(departure, schema):
    constraint = op.f("rack_dispatch_job_closure_check")
    with op.batch_alter_table(TABLE, schema=schema) as batch:
        batch.drop_constraint(constraint, type_="check")
        batch.create_check_constraint(constraint, CLOSURE.format(departure=departure))


def upgrade():
    _replace_check("closure_reason IN ('DEPARTURE_ACK','RACK_ALREADY_IN_STORAGE')", _schema())


def downgrade():
    schema = _schema()
    table = sa.table(TABLE, sa.column("closure_reason"), schema=schema)
    if op.get_bind().scalar(
        sa.select(sa.literal(True))
        .select_from(table)
        .where(table.c.closure_reason == "RACK_ALREADY_IN_STORAGE")
        .limit(1)
    ):
        raise CommandError(
            "CTU03 storage releases exist; downgrade would invalidate their evidence"
        )
    _replace_check("closure_reason='DEPARTURE_ACK'", schema)
