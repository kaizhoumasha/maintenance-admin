import asyncio
from importlib import import_module

from alembic import context
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.businesses.catalog import load_business_models
from app.core import get_settings
from app.database import SCHEMA
from app.database import create_database_engine as create_async_engine
from app.models import Base

load_business_models()
import_module("app.businesses.smt_auto.models")
target_metadata = Base.metadata
url = get_settings().DATABASE_URL

PROJECT_TABLES = (
    "audit_logs",
    "audit_logs_archive",
    "callback_receipt",
    "callback_snapshot",
    "menu",
    "permission",
    "reliable_message",
    "role",
    "role_permission",
    "transport_task",
    "user",
    "user_role",
    "alembic_version",
)


def _prepare_schema(connection) -> str:
    target = connection.scalar(text("SELECT to_regclass('enterprise_backend.alembic_version')"))
    legacy = connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
    command = getattr(context.config.cmd_opts, "cmd", None)
    if command is None:
        raise CommandError("PostgreSQL migration requires an explicit Alembic CLI command")
    changing = command[0].__name__ in {"upgrade", "downgrade", "stamp"}
    # 外部系统可能也有同名 public.alembic_version 且列不同:没有 version_num 就当作不是本项目的版本表
    ledger = (
        not target
        and legacy
        and connection.scalar(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'alembic_version' AND column_name = 'version_num'"
            )
        )
    )
    versions = (
        list(connection.scalars(text("SELECT version_num FROM public.alembic_version")))
        if ledger
        else []
    )
    known = {
        revision.revision
        for revision in ScriptDirectory.from_config(context.config).walk_revisions()
    }
    owned_legacy = bool(versions) and all(version in known for version in versions)
    if not changing:
        connection.exec_driver_sql("SET LOCAL search_path TO public")
        connection.dialect.default_schema_name = "public"
        return "public" if owned_legacy else SCHEMA
    if owned_legacy and versions != ["a023a20261024"]:
        raise CommandError("legacy public schema must first be upgraded to a023a20261024")
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    if owned_legacy:
        tables = list(
            connection.scalars(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public' AND tablename=ANY(:names)"
                ),
                {"names": list(PROJECT_TABLES)},
            )
        )
        if set(tables) != set(PROJECT_TABLES):
            raise CommandError("legacy public schema is incomplete; no project tables moved")
        for table in tables:
            if connection.scalar(text("SELECT to_regclass(:name)"), {"name": f"{SCHEMA}.{table}"}):
                raise CommandError(f"schema collision: {SCHEMA}.{table}")
        for table in tables:
            connection.exec_driver_sql(f'ALTER TABLE public."{table}" SET SCHEMA "{SCHEMA}"')
    connection.exec_driver_sql(f'SET LOCAL search_path TO "{SCHEMA}"')
    connection.dialect.default_schema_name = SCHEMA
    return SCHEMA


def _include_name(name, kind, parent):
    if kind == "schema":
        return name == SCHEMA
    # This ledger is managed by the integration operator, outside Alembic.
    return kind != "table" or name not in {
        "coarse_sorting_mock_round",
        "integration_history_retirement",
    }


def _retired_table_filter(connection, schema):
    retired = schema == SCHEMA and connection.scalar(
        text("SELECT to_regclass('enterprise_backend.integration_history_retirement') IS NOT NULL")
    )

    def include(obj, name, kind, reflected, compare_to):
        return not (
            retired
            and kind == "table"
            and name in {"integration_exchange", "integration_record", "integration_reference"}
        )

    return include


def _run(connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    with connection.begin():
        if sqlite:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        schema = None if sqlite else _prepare_schema(connection)
        options = context.get_x_argument(as_dictionary=True)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transactional_ddl=True if sqlite else None,
            version_table_schema=schema,
            include_schemas=not sqlite,
            include_name=_include_name,
            include_object=_retired_table_filter(connection, schema),
        )
        with context.begin_transaction():
            context.run_migrations()
        command = getattr(context.config.cmd_opts, "cmd", None)
        if command and command[0].__name__ == "upgrade":
            use_runtime = (
                options.get("fence_legacy_writes") == "true"
                or get_settings().INTEGRATION_RUNTIME_AUTHORITY
            )
            from app.integrations.runtime_authority import QUERY_REVISION, check_reconciled

            if not use_runtime and context.get_context().get_current_revision() != QUERY_REVISION:
                return
            try:
                check_reconciled(connection, use_runtime=use_runtime)
            except RuntimeError as error:
                raise CommandError(str(error)) from None


async def _run_online() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    if make_url(url).get_backend_name() == "postgresql":
        raise CommandError(
            "PostgreSQL schema migration requires online ownership checks; omit --sql"
        )
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
