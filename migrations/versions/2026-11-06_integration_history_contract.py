"""Independently retire reconciled legacy storage; ship only after artifact A acceptance."""

import sqlalchemy as sa
from alembic import context, op
from alembic.util import CommandError

from migrations.integration_history_contract_v2 import RETIRED, maintenance_gate, validate_contract
from migrations.integration_history_legacy_schema_v2 import LEGACY_DDL
from migrations.integration_history_views_v1 import create_views, drop_views

revision = "integrationhistorycontract"
down_revision = "integrationhistoryauthority"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    populated = any(
        bind.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {name})"))
        for name in (
            *RETIRED,
            "integration_exchange",
            "integration_record",
            "reliable_message",
            "transport_task",
        )
    )
    path = context.get_x_argument(as_dictionary=True).get("integration_history_contract_evidence")
    maintenance_gate(bind, path, populated=populated)
    task, normalized = validate_contract(bind)
    for identity, value in normalized:
        bind.execute(task.update().where(task.c.id == identity).values(submission_acceptance=value))
    drop_views(bind)
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    with op.batch_alter_table("reliable_message", schema=schema) as batch:
        for column in ("request_raw", "response_raw", "last_response_raw"):
            batch.drop_column(column)
    for name in RETIRED:
        op.drop_table(name, schema=schema)
    create_views(bind)


def downgrade():
    bind = op.get_bind()
    if any(
        bind.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {name})"))
        for name in (
            "integration_exchange",
            "integration_record",
            "integration_reference",
            "reliable_message",
            "transport_task",
        )
    ):
        raise CommandError("refusing downgrade: preserve new authoritative facts")
    drop_views(bind)
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    with op.batch_alter_table("reliable_message", schema=schema) as batch:
        for column in ("request_raw", "response_raw", "last_response_raw"):
            batch.add_column(sa.Column(column, sa.LargeBinary(), nullable=True))
    for ddl in LEGACY_DDL[bind.dialect.name]:
        bind.exec_driver_sql(ddl)
    if schema:
        value = bind.execute(
            sa.text("SELECT last_value,is_called FROM integration_receipt_id_seq")
        ).one()
        bind.execute(
            sa.text("SELECT setval('callback_receipt_id_seq',:value,:called)"),
            dict(value=value.last_value, called=value.is_called),
        )
    create_views(bind)
