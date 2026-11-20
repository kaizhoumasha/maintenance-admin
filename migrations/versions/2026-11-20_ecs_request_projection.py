"""Allow ECS synchronous requests in the HTTP projection."""

from alembic import op

from migrations.http_exchange_views_v1 import create_http_views

revision = "ecs_request_projection_v1"
down_revision = "pending_scan_indexes_v1"
branch_labels = None
depends_on = None

CHECK = "ecs_http_exchange_interaction_kind_check"
NEW_KINDS = "interaction_kind IN ('COMMAND', 'RESULT', 'EVENT', 'STATUS', 'REQUEST')"
OLD_KINDS = "interaction_kind IN ('COMMAND', 'RESULT', 'EVENT', 'STATUS')"


def _replace(expression):
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    schema = "enterprise_backend" if pg else None
    if not pg:
        bind.exec_driver_sql("DROP VIEW IF EXISTS integration_record_summary")
        bind.exec_driver_sql("DROP VIEW IF EXISTS integration_exchange_summary")
    with op.batch_alter_table("ecs_http_exchange", schema=schema) as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), expression)
    create_http_views(bind, initiator=True, replace=pg)


def upgrade():
    _replace(NEW_KINDS)


def downgrade():
    _replace(OLD_KINDS)
