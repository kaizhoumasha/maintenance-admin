"""Remove the two inactive SMT task sources after the authority cutover is verified."""

from importlib import import_module

import sqlalchemy as sa
from alembic import context, op
from alembic.util import CommandError

revision = "sorting_task_cleanup"
down_revision = "sorting_task_authority"
branch_labels = None
depends_on = None

authority = import_module("migrations.versions.2026-11-09_sorting_task_authority")
MANUAL_COLUMNS = (
    "task_id",
    "task_type",
    "business_type",
    "work_line_id",
    "workline_code",
    "issued_operation_id",
    "issued_timestamp",
    "issued_data",
    "queue_revision",
    "dispatch_sequence",
    "not_before",
    "status",
    "plan_revision",
    "prepare_operation_id",
    "prepare_message_id",
    "prepare_accepted",
    "prepare_response_timestamp",
    "prepare_accepted_at_ms",
    "confirmation_operation_id",
    "received_work",
    "target_rack",
)
AUTO_COLUMNS = (
    "task_id",
    "task_type",
    "business_type",
    "work_line_id",
    "workline_code",
    "issued_operation_id",
    "issued_timestamp",
    "issued_data",
    "status",
    "queue_revision",
    "dispatch_sequence",
    "not_before_ms",
    "plan_revision",
    "prepare_accepted_at_ms",
    "confirmation_next_check_at_ms",
    "confirmation_basis",
    "closed_at_ms",
    "cancel_reason",
    "direct_obligations_closed_at_ms",
    "details_cleaned_at_ms",
    "details_cleaned",
    "pending",
    "next_check_at_ms",
    "first_plan_wait_ms",
    "first_plan_deadline_ms",
    "rack_retention_basis",
)
FROZEN_FIELDS = (
    "task_type",
    "business_type",
    "work_line_id",
    "workline_code",
    "issued_operation_id",
    "issued_timestamp",
)


def upgrade():
    bind = op.get_bind()
    schema = authority.schema_for(bind)
    if schema is not None:
        bind.exec_driver_sql(
            f"LOCK TABLE {schema}.smt_manual_task, {schema}.smt_auto_task, "
            f"{schema}.sorting_task IN ACCESS EXCLUSIVE MODE NOWAIT"
        )
    manual, auto, shared = authority._tables(bind, schema)
    populated = any(
        bind.scalar(sa.select(t.c.task_id).limit(1)) is not None for t in (manual, auto)
    )
    if populated and (
        context.get_x_argument(as_dictionary=True).get("sorting_task_cleanup_verified") != "true"
    ):
        raise CommandError(
            "Verify sorting-task cutover, then pass -x sorting_task_cleanup_verified=true"
        )
    for old in (manual, auto):
        if (
            bind.scalar(
                sa.select(old.c.task_id)
                .outerjoin(shared, shared.c.task_id == old.c.task_id)
                .where(
                    sa.or_(
                        shared.c.task_id.is_(None),
                        *(old.c[key].is_distinct_from(shared.c[key]) for key in FROZEN_FIELDS),
                    )
                )
                .limit(1)
            )
            is not None
        ):
            raise CommandError(f"Original identity missing or changed for {old.name}")
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names(schema=schema):
        if any(
            fk["referred_table"] in authority.OLD_TABLES
            for fk in inspector.get_foreign_keys(table, schema=schema)
        ):
            raise CommandError(f"Legacy SMT task reference remains in {table}")
    for name in authority.OLD_TABLES:
        op.drop_table(name, schema=schema)


def _restore(bind, shared, schema, *, mode, name, names):
    manual = mode == "MANUAL"
    columns = [shared.c["not_before_ms" if key == "not_before" else key] for key in names]
    defaults = {
        "task_type": "AUTO",
        "status": "QUEUED",
        "queue_revision": "1",
        "plan_revision": "0",
        "details_cleaned": sa.false(),
        "pending": sa.true(),
    }
    types_without_limit = {
        "task_type",
        "business_type",
        "workline_code",
        "issued_operation_id",
        "status",
    }
    table = op.create_table(
        name,
        *(
            sa.Column(
                key,
                sa.String() if manual and key in types_without_limit else c.type,
                nullable=c.nullable,
                server_default=None if manual else defaults.get(key),
            )
            for key, c in zip(names, columns, strict=True)
        ),
        sa.PrimaryKeyConstraint("task_id", name="smt_manual_task_pkey" if manual else None),
        *(
            ()
            if manual
            else (
                sa.CheckConstraint("task_type = 'AUTO'", name="smt_auto_task_type"),
                sa.CheckConstraint(
                    "business_type IN ('inbound','outbound')", name="smt_auto_task_business_type"
                ),
                sa.CheckConstraint(
                    "status IN ('QUEUED','PREPARING','EXECUTING','COMPLETED','CANCELLED')",
                    name="smt_auto_task_status",
                ),
                sa.CheckConstraint(
                    "queue_revision >= 1 AND plan_revision >= 0", name="smt_auto_task_revisions"
                ),
            )
        ),
        schema=schema,
    )
    active = sa.text("status IN ('PREPARING', 'EXECUTING')" if manual else authority.ACTIVE)
    op.create_index(
        "smt_manual_execution_slot" if manual else "smt_auto_task_execution_slot",
        name,
        ["work_line_id"],
        unique=True,
        postgresql_where=active,
        sqlite_where=active,
        schema=schema,
    )
    if not manual:
        op.create_index(
            "smt_auto_task_queue",
            name,
            ["work_line_id", "dispatch_sequence", "issued_timestamp", "task_id"],
            schema=schema,
        )
    bind.execute(
        table.insert().from_select(names, sa.select(*columns).where(shared.c.task_type == mode))
    )


def downgrade():
    bind = op.get_bind()
    schema = authority.schema_for(bind)
    if schema is not None:
        bind.exec_driver_sql(f"LOCK TABLE {schema}.sorting_task IN ACCESS EXCLUSIVE MODE NOWAIT")
    shared = sa.Table(authority.NEW_TABLE, sa.MetaData(), schema=schema, autoload_with=bind)
    _restore(
        bind, shared, schema, mode="MANUAL", name=authority.OLD_TABLES[0], names=MANUAL_COLUMNS
    )
    _restore(bind, shared, schema, mode="AUTO", name=authority.OLD_TABLES[1], names=AUTO_COLUMNS)
