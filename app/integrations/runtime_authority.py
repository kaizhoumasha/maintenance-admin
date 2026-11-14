from sqlalchemy import text

from app.core.config import get_settings

PREVIOUS_QUERY_REVISION = "http_exchange_query_views"
QUERY_REVISION = "http_exchange_initiator"
LEGACY_TABLES = ("integration_exchange", "integration_record", "integration_reference")


def enabled() -> bool:
    return get_settings().INTEGRATION_RUNTIME_AUTHORITY


def check_reconciled(connection, *, use_runtime, query_revision=QUERY_REVISION):
    postgres = connection.dialect.name == "postgresql"
    retired = False
    has_runtime_tables = False
    if postgres:
        retired, has_runtime_tables = connection.execute(
            text("""
            SELECT to_regclass('enterprise_backend.integration_history_retirement') IS NOT NULL,
                   to_regclass('enterprise_backend.wms_http_exchange') IS NOT NULL
        """)
        ).one()
        present = connection.scalar(
            text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='enterprise_backend' AND c.relkind IN ('r','p')
              AND c.relname IN ('integration_exchange','integration_record','integration_reference')
        """)
        )
        fences = connection.scalar(
            text("""
            SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='enterprise_backend'
              AND c.relname IN ('integration_exchange','integration_record','integration_reference')
              AND t.tgname=c.relname||'_runtime_fence'
        """)
        )
    else:
        present = connection.scalar(
            text("""
            SELECT count(*) FROM sqlite_master WHERE type='table'
              AND name IN ('integration_exchange','integration_record','integration_reference')
        """)
        )
        fences = connection.scalar(
            text("""
            SELECT count(*) FROM sqlite_master WHERE type='trigger'
              AND tbl_name IN ('integration_exchange','integration_record','integration_reference')
              AND name IN (tbl_name||'_runtime_fence_insert', tbl_name||'_runtime_fence_update',
                           tbl_name||'_runtime_fence_delete')
        """)
        )
    if not use_runtime:
        if retired or fences or (has_runtime_tables or present) and present != len(LEGACY_TABLES):
            raise RuntimeError("database requires INTEGRATION_RUNTIME_AUTHORITY=true")
        # HTTP capture also runs in legacy authority mode.
        if query_revision == QUERY_REVISION and _has_http_direction(connection):
            raise RuntimeError("HTTP exchanges require the initiator migration")
        return
    ledger = "enterprise_backend.alembic_version" if postgres else "alembic_version"
    if not postgres:
        has_runtime_tables = bool(
            connection.scalar(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'")
            )
        )
    if (
        not has_runtime_tables
        or connection.scalar(text(f"SELECT version_num FROM {ledger}")) != query_revision
    ):
        raise RuntimeError("integration runtime authority requires successful reconciliation")
    if retired:
        proof = connection.scalar(
            text("""
            SELECT EXISTS (SELECT 1 FROM enterprise_backend.integration_history_retirement)
        """)
        )
        if present or not proof:
            raise RuntimeError("integration history retirement state is inconsistent")
        return
    if postgres:
        fences = connection.scalar(
            text("""
            SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='enterprise_backend' AND t.tgenabled IN ('O','A')
              AND c.relname IN ('integration_exchange','integration_record','integration_reference')
              AND t.tgname=c.relname||'_runtime_fence' AND t.tgtype=62
              AND t.tgfoid=to_regprocedure('enterprise_backend.block_retired_integration_write()')
        """)
        )
    if present != len(LEGACY_TABLES) or fences != (3 if postgres else 9):
        raise RuntimeError("integration runtime authority requires fencing all legacy writers")


def _has_http_direction(connection):
    if connection.dialect.name == "postgresql":
        return connection.scalar(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema='enterprise_backend' AND column_name='direction'
                  AND table_name IN (
                      'wms_http_exchange', 'ecs_http_exchange', 'transport_http_exchange'
                  )
            )
        """)
        )
    return any(
        any(
            row["name"] == "direction"
            for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").mappings()
        )
        for table in ("wms_http_exchange", "ecs_http_exchange", "transport_http_exchange")
    )


async def require_reconciled(factory):
    async with factory() as session:
        connection = await session.connection()
        await connection.run_sync(check_reconciled, use_runtime=enabled())
