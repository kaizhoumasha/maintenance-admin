"""Move SMT task authority and task references to one table."""

import sqlalchemy as sa
from alembic import op
from alembic.util import CommandError

revision = "sorting_task_authority"
down_revision = "xray_pending_due"
branch_labels = None
depends_on = None

OLD_TABLES = ("smt_manual_task", "smt_auto_task")
NEW_TABLE = "sorting_task"
AUTO_REFERENCES = (
    "smt_auto_task_input",
    "smt_auto_task_request",
    "smt_auto_supply_member",
    "smt_auto_inbound_source_member",
    "smt_auto_outbound_source_member",
    "smt_auto_bin_work",
    "smt_auto_inbound_plate",
    "smt_auto_inbound_decision_round",
    "smt_auto_outbound_plate",
    "smt_auto_outbound_decision_round",
)
FK_NAMING = {"fk": "%(table_name)s_%(column_0_name)s_fkey"}
ACTIVE = "status IN ('PREPARING','EXECUTING')"


def schema_for(bind):
    return "enterprise_backend" if bind.dialect.name == "postgresql" else None


def _lock_sources(bind, schema):
    if schema is not None:
        bind.exec_driver_sql(
            f"LOCK TABLE {schema}.smt_manual_task, {schema}.smt_auto_task "
            "IN ACCESS EXCLUSIVE MODE NOWAIT"
        )


def move_foreign_keys(bind, schema, source, target):
    inspector = sa.inspect(bind)
    for name in AUTO_REFERENCES:
        references = [
            fk
            for fk in inspector.get_foreign_keys(name, schema=schema)
            if fk["constrained_columns"] == ["task_id"] and fk["referred_table"] == source
        ]
        if len(references) != 1:
            raise CommandError(f"Expected exactly one {name}.task_id reference to {source}")
        fk = references[0]
        fk_name = fk["name"] or f"{name}_task_id_fkey"
        target_column = "auto_task_id" if target == NEW_TABLE else "task_id"
        with op.batch_alter_table(name, schema=schema, naming_convention=FK_NAMING) as batch:
            batch.drop_constraint(fk_name, type_="foreignkey")
            batch.create_foreign_key(
                fk_name,
                target,
                ["task_id"],
                [target_column],
                referent_schema=schema,
                **fk["options"],
            )


def _create(schema):
    op.create_table(
        NEW_TABLE,
        sa.Column("task_id", sa.String(100), primary_key=True),
        sa.Column("task_type", sa.String(20), nullable=False),
        sa.Column("business_type", sa.String(20), nullable=False),
        sa.Column("work_line_id", sa.Integer(), nullable=False),
        sa.Column("workline_code", sa.String(100), nullable=False),
        sa.Column("issued_operation_id", sa.String(200), nullable=False),
        sa.Column("issued_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("issued_data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("queue_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("dispatch_sequence", sa.BigInteger(), nullable=False),
        sa.Column("not_before_ms", sa.BigInteger()),
        sa.Column("plan_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("prepare_accepted_at_ms", sa.BigInteger()),
        sa.Column("first_plan_wait_ms", sa.BigInteger()),
        sa.Column("first_plan_deadline_ms", sa.BigInteger()),
        sa.Column("confirmation_next_check_at_ms", sa.BigInteger()),
        sa.Column("confirmation_basis", sa.JSON()),
        sa.Column("closed_at_ms", sa.BigInteger()),
        sa.Column("cancel_reason", sa.String(200)),
        sa.Column("direct_obligations_closed_at_ms", sa.BigInteger()),
        sa.Column("rack_retention_basis", sa.JSON()),
        sa.Column("details_cleaned_at_ms", sa.BigInteger()),
        sa.Column("details_cleaned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pending", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_check_at_ms", sa.BigInteger()),
        sa.Column(
            "owner_domain",
            sa.String(20),
            sa.Computed(
                "CASE task_type WHEN 'MANUAL' THEN 'smt_manual' WHEN 'AUTO' THEN 'smt_auto' END",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "auto_task_id",
            sa.String(100),
            sa.Computed("CASE WHEN task_type = 'AUTO' THEN task_id END", persisted=True),
            unique=True,
        ),
        sa.Column("prepare_operation_id", sa.String()),
        sa.Column("prepare_message_id", sa.BigInteger()),
        sa.Column("prepare_accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prepare_response_timestamp", sa.BigInteger()),
        sa.Column("confirmation_operation_id", sa.String()),
        sa.Column("received_work", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("target_rack", sa.JSON()),
        sa.CheckConstraint("task_type IN ('MANUAL', 'AUTO')", name="sorting_task_type"),
        sa.CheckConstraint(
            "business_type IN ('inbound', 'outbound')", name="sorting_task_business_type"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED','PREPARING','EXECUTING','COMPLETED','CANCELLED')",
            name="sorting_task_status",
        ),
        sa.CheckConstraint(
            "queue_revision >= 1 AND plan_revision >= 0", name="sorting_task_revisions"
        ),
        schema=schema,
    )
    op.create_index(
        "sorting_task_execution_slot",
        NEW_TABLE,
        ["work_line_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
        sqlite_where=sa.text(ACTIVE),
        schema=schema,
    )
    op.create_index(
        "sorting_task_queue",
        NEW_TABLE,
        ["work_line_id", "dispatch_sequence", "issued_timestamp", "task_id"],
        schema=schema,
    )


def _tables(bind, schema):
    metadata = sa.MetaData()
    return tuple(
        sa.Table(name, metadata, schema=schema, autoload_with=bind)
        for name in (*OLD_TABLES, NEW_TABLE)
    )


def upgrade():
    bind = op.get_bind()
    schema = schema_for(bind)
    _lock_sources(bind, schema)
    metadata = sa.MetaData()
    manual, auto = [
        sa.Table(name, metadata, schema=schema, autoload_with=bind) for name in OLD_TABLES
    ]
    for table, mode in ((manual, "MANUAL"), (auto, "AUTO")):
        if (
            bind.scalar(sa.select(table.c.task_id).where(table.c.task_type != mode).limit(1))
            is not None
        ):
            raise CommandError(f"Unexpected task mode in {table.name}")
    if (
        bind.scalar(
            sa.select(manual.c.task_id).join(auto, manual.c.task_id == auto.c.task_id).limit(1)
        )
        is not None
    ):
        raise CommandError("Cannot migrate overlapping MANUAL and AUTO task IDs")
    if (
        bind.scalar(
            sa.select(manual.c.work_line_id)
            .join(auto, manual.c.work_line_id == auto.c.work_line_id)
            .where(
                manual.c.status.in_(("PREPARING", "EXECUTING")),
                auto.c.status.in_(("PREPARING", "EXECUTING")),
            )
            .limit(1)
        )
        is not None
    ):
        raise CommandError("Cannot migrate multiple active tasks on the same workline")
    _create(schema)
    shared = sa.Table(NEW_TABLE, metadata, schema=schema, autoload_with=bind)
    for table in (manual, auto):
        columns = ["not_before_ms" if c.name == "not_before" else c.name for c in table.c]
        bind.execute(shared.insert().from_select(columns, sa.select(*table.c)))
    move_foreign_keys(bind, schema, OLD_TABLES[1], NEW_TABLE)


def downgrade():
    bind = op.get_bind()
    schema = schema_for(bind)
    _lock_sources(bind, schema)
    manual, auto, shared = _tables(bind, schema)
    if schema is not None:
        bind.exec_driver_sql(f"LOCK TABLE {schema}.sorting_task IN ACCESS EXCLUSIVE MODE NOWAIT")
    counts = [
        bind.scalar(sa.select(sa.func.count()).select_from(t)) for t in (manual, auto, shared)
    ]
    if counts[0] + counts[1] != counts[2]:
        raise CommandError("Sorting tasks changed after cutover; downgrade would lose data")
    for old in (manual, auto):
        changed = [
            sa.cast(c, sa.Text()).is_distinct_from(
                sa.cast(shared.c["not_before_ms" if c.name == "not_before" else c.name], sa.Text())
            )
            for c in old.c
        ]
        if (
            bind.scalar(
                sa.select(old.c.task_id)
                .outerjoin(shared, old.c.task_id == shared.c.task_id)
                .where(sa.or_(*changed))
                .limit(1)
            )
            is not None
        ):
            raise CommandError("Sorting tasks changed after cutover; downgrade would lose data")
    move_foreign_keys(bind, schema, NEW_TABLE, OLD_TABLES[1])
    op.drop_table(NEW_TABLE, schema=schema)
