"""Queue revisions of HTTP attempts that still need projecting."""

import sqlalchemy as sa
from alembic import op

revision = "http_projection_pending_v1"
down_revision = "user_session_version_v1"
branch_labels = None
depends_on = None

PROJECTIONS = {
    "wms_http_exchange": ("wms-decisions", "wms-facts"),
    "ecs_http_exchange": ("ecs-command",),
    "transport_http_exchange": ("wms-transport",),
}


def upgrade():
    postgres = op.get_bind().dialect.name == "postgresql"
    schema = "enterprise_backend" if postgres else None
    prefix = f"{schema}." if schema else ""
    queue = f"{prefix}http_projection_pending"
    attempts = f"{prefix}reliable_delivery_attempt"
    op.create_table(
        "http_projection_pending",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.ForeignKeyConstraint(["attempt_id"], [f"{attempts}.id"], ondelete="CASCADE"),
        schema=schema,
    )
    for table, protocols in PROJECTIONS.items():
        op.execute(
            sa.text(f"""
            INSERT INTO {queue} (attempt_id)
            SELECT a.id FROM {attempts} a
            WHERE a.protocol IN :protocols AND a.endpoint_path <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM {prefix}{table} p
                  WHERE p.id = a.id AND p.source_revision >= a.revision
              )
            """).bindparams(sa.bindparam("protocols", protocols, expanding=True))
        )
    predicate = """NEW.endpoint_path <> '' AND NEW.protocol IN
        ('wms-decisions', 'wms-facts', 'wms-transport', 'ecs-command')"""
    if postgres:
        op.execute(f"""
            CREATE FUNCTION {prefix}enqueue_http_projection() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO {queue} (attempt_id) VALUES (NEW.id)
                ON CONFLICT (attempt_id) DO NOTHING;
                RETURN NEW;
            END;
            $$
            """)
        op.execute(f"""
            CREATE TRIGGER http_projection_enqueue
            AFTER INSERT OR UPDATE OF revision ON {attempts}
            FOR EACH ROW WHEN ({predicate})
            EXECUTE FUNCTION {prefix}enqueue_http_projection()
            """)
    else:
        for action, suffix in (("INSERT", "insert"), ("UPDATE OF revision", "update")):
            op.execute(f"""
                CREATE TRIGGER http_projection_enqueue_{suffix}
                AFTER {action} ON {attempts} WHEN {predicate}
                BEGIN
                    INSERT INTO {queue} (attempt_id) VALUES (NEW.id)
                    ON CONFLICT (attempt_id) DO NOTHING;
                END
                """)


def downgrade():
    postgres = op.get_bind().dialect.name == "postgresql"
    schema = "enterprise_backend" if postgres else None
    if postgres:
        op.execute(
            "DROP TRIGGER http_projection_enqueue ON enterprise_backend.reliable_delivery_attempt"
        )
        op.execute("DROP FUNCTION enterprise_backend.enqueue_http_projection()")
    else:
        for suffix in ("insert", "update"):
            op.execute(f"DROP TRIGGER http_projection_enqueue_{suffix}")
    op.drop_table("http_projection_pending", schema=schema)
