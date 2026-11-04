"""Add bounded EVENT evidence and immutable scheduling-scope metadata."""

import sqlalchemy as sa
from alembic import op

revision = "rollerfoundation"
down_revision = "sharedrackdispatch"
branch_labels = None
depends_on = None

SCOPE = (
    "(scope_owner IS NULL AND scope_kind IS NULL AND scope_id IS NULL) OR (scope_owner IS "
    "NOT NULL AND scope_owner != '' AND scope_kind IS NOT NULL AND scope_kind IN "
    "('TASK','LINE') AND scope_id IS NOT NULL AND scope_id != '')"
)
OLD_STATUS = "status IN ('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED')"
NEW_STATUS = (
    "status IN ('PENDING','SENDING','STALLED','HELD','DONE','FAILED','EXPIRED','ABANDONED')"
)
OLD_SCHEDULE = (
    "(status IN ('STALLED','HELD') AND next_attempt_at IS NULL) OR (status NOT IN "
    "('STALLED','HELD') AND next_attempt_at IS NOT NULL)"
)
NEW_SCHEDULE = (
    "(status IN ('STALLED','HELD','ABANDONED') AND next_attempt_at IS NULL) OR (status NOT "
    "IN ('STALLED','HELD','ABANDONED') AND next_attempt_at IS NOT NULL)"
)
OLD_CLOSURE = (
    "(state IN ('ACTIVE','RELEASING') AND pending AND closure_reason IS NULL AND "
    "closure_basis IS NULL) OR (NOT pending AND closure_reason IS NOT NULL AND closure_basis "
    "IS NOT NULL AND ((state='COMPLETED' AND (closure_reason='LEGACY_CLOSED' OR "
    "(closure_reason='DEPARTURE_ACK' AND completed_at_ms IS NOT NULL))) OR (state IN "
    "('WITHDRAWN','NOT_ARRIVED') AND closure_reason=state)))"
)
NEW_CLOSURE = OLD_CLOSURE.replace(
    "'WITHDRAWN','NOT_ARRIVED'", "'WITHDRAWN','NOT_ARRIVED','CLOSED_BY_SCOPE'"
)


def _schema(bind):
    return None if bind.dialect.name == "sqlite" else bind.dialect.default_schema_name


def _checks(table, schema, expressions):
    with op.batch_alter_table(table, schema=schema) as batch:
        for name, expression in expressions.items():
            key = op.f(f"{table}_{name}_check")
            batch.drop_constraint(key, type_="check")
            batch.create_check_constraint(key, expression)


def upgrade():
    schema = _schema(op.get_bind())
    for table in ("reliable_message", "rack_dispatch_job"):
        with op.batch_alter_table(table, schema=schema) as batch:
            for column in ("scope_owner", "scope_kind", "scope_id"):
                batch.add_column(
                    sa.Column(
                        column, sa.String(4) if column == "scope_kind" else sa.Text(), nullable=True
                    )
                )
            name = "scope" if table == "reliable_message" else "execution_scope"
            batch.create_check_constraint(op.f(f"{table}_{name}_check"), SCOPE)
            batch.create_index(
                f"{table}_{'scope_idx' if table == 'reliable_message' else 'execution_scope'}",
                ["scope_owner", "scope_kind", "scope_id"],
            )
            if table == "reliable_message":
                batch.add_column(
                    sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True)
                )
    with op.batch_alter_table("callback_receipt", schema=schema) as batch:
        for column in ("event_envelope", "event_context", "application_ref"):
            batch.add_column(sa.Column(column, sa.JSON(none_as_null=True), nullable=True))
    _checks("reliable_message", schema, {"status": NEW_STATUS, "next_attempt": NEW_SCHEDULE})
    _checks("rack_dispatch_job", schema, {"closure": NEW_CLOSURE})


def downgrade():
    bind = op.get_bind()
    schema = _schema(bind)
    metadata = sa.MetaData()
    # Validate all new semantics before any schema mutation.
    for name, columns, extra in (
        (
            "reliable_message",
            ("scope_owner", "scope_kind", "scope_id", "dispatch_started_at"),
            "status = 'ABANDONED'",
        ),
        (
            "rack_dispatch_job",
            ("scope_owner", "scope_kind", "scope_id"),
            "state = 'CLOSED_BY_SCOPE'",
        ),
        ("callback_receipt", ("event_envelope", "event_context", "application_ref"), "false"),
    ):
        table = sa.Table(name, metadata, schema=schema, autoload_with=bind)
        condition = sa.or_(*(table.c[key].is_not(None) for key in columns), sa.text(extra))
        if bind.scalar(sa.select(sa.literal(1)).select_from(table).where(condition).limit(1)):
            raise RuntimeError(
                "roller foundation downgrade would discard new evidence or scope semantics"
            )
    _checks("reliable_message", schema, {"status": OLD_STATUS, "next_attempt": OLD_SCHEDULE})
    _checks("rack_dispatch_job", schema, {"closure": OLD_CLOSURE})
    for table in ("reliable_message", "rack_dispatch_job"):
        with op.batch_alter_table(table, schema=schema) as batch:
            batch.drop_index(
                f"{table}_{'scope_idx' if table == 'reliable_message' else 'execution_scope'}"
            )
            name = "scope" if table == "reliable_message" else "execution_scope"
            batch.drop_constraint(op.f(f"{table}_{name}_check"), type_="check")
            if table == "reliable_message":
                batch.drop_column("dispatch_started_at")
            for column in ("scope_owner", "scope_kind", "scope_id"):
                batch.drop_column(column)
    with op.batch_alter_table("callback_receipt", schema=schema) as batch:
        for column in ("event_envelope", "event_context", "application_ref"):
            batch.drop_column(column)
