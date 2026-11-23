"""Switch metadata views to HTTP exchange tables and independent runtime facts."""

from alembic import op
from alembic.util import CommandError
from sqlalchemy import text

from migrations.http_exchange_views_v1 import create_http_views
from migrations.integration_history_views_v1 import VIEWS, create_views

revision = "http_exchange_query_views"
down_revision = "http_exchange_runtime_authority"
branch_labels = None
depends_on = None
HTTP_TABLES = ("wms_http_exchange", "ecs_http_exchange", "transport_http_exchange")


def _replace(create):
    bind = op.get_bind()
    pg = bind.dialect.name == "postgresql"
    grants = []
    if pg:
        grants = bind.execute(
            text("""
            SELECT c.relname,
                   CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE quote_ident(r.rolname) END,
                   a.is_grantable,
                   a.grantee = (CURRENT_USER::regrole)::oid
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(coalesce(c.relacl, acldefault('r',c.relowner))) a
            LEFT JOIN pg_roles r ON r.oid=a.grantee
            WHERE n.nspname='enterprise_backend'
              AND c.relname IN ('integration_exchange_summary','integration_record_summary')
              AND a.privilege_type='SELECT'
        """)
        ).all()
    prefix = "enterprise_backend." if pg else ""
    for name in reversed(VIEWS):
        bind.exec_driver_sql(f"DROP VIEW IF EXISTS {prefix}{name}")
    create(bind)
    if pg:
        for name, role, grantable, is_creator in grants:
            if name in VIEWS and not is_creator:
                suffix = " WITH GRANT OPTION" if grantable else ""
                bind.exec_driver_sql(f"GRANT SELECT ON enterprise_backend.{name} TO {role}{suffix}")


def upgrade():
    schema = "enterprise_backend" if op.get_bind().dialect.name == "postgresql" else None
    condition = text("replayed_from_id IS NOT NULL")
    for name in HTTP_TABLES:
        op.create_index(
            f"{name}_replay_idx",
            name,
            ["replayed_from_id"],
            schema=schema,
            postgresql_where=condition,
            sqlite_where=condition,
        )
    _replace(create_http_views)


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql" and bind.scalar(
        text("SELECT to_regclass('enterprise_backend.integration_history_retirement') IS NOT NULL")
    ):
        raise CommandError(
            "retired integration history requires restoring the pre-retirement backup"
        )
    _replace(create_views)
    schema = "enterprise_backend" if bind.dialect.name == "postgresql" else None
    for name in HTTP_TABLES:
        op.drop_index(f"{name}_replay_idx", table_name=name, schema=schema)
