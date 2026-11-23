"""Name the HTTP request initiator independently of warehouse flow."""

from alembic import op

from migrations.http_exchange_views_v1 import create_http_views

revision = "http_exchange_initiator"
down_revision = "http_exchange_query_views"
branch_labels = None
depends_on = None

TABLES = ("wms_http_exchange", "ecs_http_exchange", "transport_http_exchange")
VIEWS = ("integration_exchange_summary", "integration_record_summary")


def _convert(*, old, new, mapping, initiator):
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    schema = "enterprise_backend" if pg else None
    prefix = f"{schema}." if pg else ""
    if not pg:
        for view in reversed(VIEWS):
            bind.exec_driver_sql(f"DROP VIEW {view}")

    old_peer, new_peer = mapping[0]
    old_wes, new_wes = mapping[1]
    for table in TABLES:
        with op.batch_alter_table(table, schema=schema) as batch:
            batch.drop_constraint(op.f(f"{table}_{old}_check"), type_="check")
            batch.alter_column(old, new_column_name=new)
        bind.exec_driver_sql(
            f"UPDATE {prefix}{table} SET {new} = CASE {new} "
            f"WHEN '{old_peer}' THEN '{new_peer}' WHEN '{old_wes}' THEN '{new_wes}' END"
        )
        with op.batch_alter_table(table, schema=schema) as batch:
            batch.create_check_constraint(
                op.f(f"{table}_{new}_check"), f"{new} IN ('{new_peer}', '{new_wes}')"
            )

    if pg:
        # In-place replacement retains the views' identities, grants and dependents.
        for view in VIEWS:
            bind.exec_driver_sql(f"ALTER VIEW {prefix}{view} RENAME COLUMN {old} TO {new}")
    create_http_views(bind, initiator=initiator, replace=pg)


def upgrade():
    _convert(
        old="direction",
        new="initiator",
        mapping=(("INBOUND", "PEER"), ("OUTBOUND", "WES")),
        initiator=True,
    )


def downgrade():
    _convert(
        old="initiator",
        new="direction",
        mapping=(("PEER", "INBOUND"), ("WES", "OUTBOUND")),
        initiator=False,
    )
