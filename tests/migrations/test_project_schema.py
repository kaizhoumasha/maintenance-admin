import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import create_database_engine
from app.models import Permission


async def migrate(url, revision):
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", revision],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.fixture
async def schema_database(pg_url):
    database = "schema_test_" + uuid4().hex
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = make_url(pg_url).set(database=database).render_as_string(hide_password=False)
    engine = create_async_engine(url)
    try:
        yield url, engine
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        await admin.dispose()


async def legacy_database(url, engine):
    result = await migrate(url, "a023a20261024")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.begin() as connection:
        tables = list(
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname='enterprise_backend'")
            )
        )
        for table in tables:
            await connection.execute(
                text(f'ALTER TABLE enterprise_backend."{table}" SET SCHEMA public')
            )
        await connection.execute(
            text("CREATE TABLE public.other_system (id integer PRIMARY KEY, payload text)")
        )
        await connection.execute(text("INSERT INTO public.other_system VALUES (1, 'keep me')"))
        await connection.execute(
            text(
                "INSERT INTO public.permission (code, name) "
                "VALUES ('test:schema:preserve', 'keep permission')"
            )
        )


async def test_upgrade_moves_owned_objects_and_preserves_foreign_tables(schema_database):
    url, engine = schema_database
    await legacy_database(url, engine)
    current = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "current"],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert current.returncode == 0, current.stdout + current.stderr
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('public.alembic_version')"))
        assert (
            await connection.scalar(
                text("SELECT to_regclass('enterprise_backend.alembic_version')")
            )
            is None
        )
        table_oid = await connection.scalar(text("SELECT 'public.permission'::regclass::oid"))
        sequence_oid = await connection.scalar(
            text("SELECT pg_get_serial_sequence('public.permission','id')::regclass::oid")
        )
        foreign_oid = await connection.scalar(text("SELECT 'public.other_system'::regclass::oid"))
    result = await migrate(url, "head")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT 'enterprise_backend.permission'::regclass::oid"))
            == table_oid
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT pg_get_serial_sequence('enterprise_backend.permission','id')"
                    "::regclass::oid"
                )
            )
            == sequence_oid
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT name FROM enterprise_backend.permission "
                    "WHERE code='test:schema:preserve'"
                )
            )
            == "keep permission"
        )
        assert await connection.scalar(text("SELECT to_regclass('public.permission')")) is None
        assert (
            await connection.scalar(text("SELECT 'public.other_system'::regclass::oid"))
            == foreign_oid
        )
        assert await connection.scalar(text("SELECT payload FROM public.other_system")) == "keep me"
        assert await connection.scalar(text("SELECT to_regclass('public.alembic_version')")) is None
        assert await connection.scalar(
            text("SELECT version_num FROM enterprise_backend.alembic_version")
        )


@pytest.mark.parametrize("unsafe_state", ["collision", "missing_table"])
async def test_unsafe_schema_aborts_without_moving_any_project_table(schema_database, unsafe_state):
    url, engine = schema_database
    await legacy_database(url, engine)
    async with engine.begin() as connection:
        if unsafe_state == "collision":
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS enterprise_backend"))
            await connection.execute(
                text("CREATE TABLE enterprise_backend.permission (marker text)")
            )
        else:
            await connection.execute(text("DROP TABLE public.callback_snapshot"))
    result = await migrate(url, "head")
    assert result.returncode != 0
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('public.permission')"))
        assert await connection.scalar(text("SELECT to_regclass('public.role')"))
        assert (
            await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "a023a20261024"
        )
        assert await connection.scalar(text("SELECT payload FROM public.other_system")) == "keep me"


async def test_programmatic_inspection_cannot_implicitly_move_legacy_tables(schema_database):
    url, engine = schema_database
    await legacy_database(url, engine)
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            "from alembic import command; from alembic.config import Config; "
            "command.current(Config('alembic.ini'))",
        ],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "explicit Alembic CLI command" in result.stdout + result.stderr
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('public.permission')"))
        assert (
            await connection.scalar(text("SELECT to_regclass('enterprise_backend.permission')"))
            is None
        )


async def test_fresh_database_keeps_another_systems_public_migration_ledger(schema_database):
    url, engine = schema_database
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE public.alembic_version (version_num text)"))
        await connection.execute(
            text("INSERT INTO public.alembic_version VALUES ('foreign_revision')")
        )
        await connection.execute(text("CREATE TABLE public.permission (marker text)"))
        await connection.execute(
            text("INSERT INTO public.permission VALUES ('foreign permission')")
        )
    current = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "current"],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert current.returncode == 0, current.stdout + current.stderr
    result = await migrate(url, "head")
    assert result.returncode == 0, result.stdout + result.stderr
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM public.alembic_version"))
            == "foreign_revision"
        )
        assert (
            await connection.scalar(text("SELECT marker FROM public.permission"))
            == "foreign permission"
        )
        assert await connection.scalar(text("SELECT to_regclass('enterprise_backend.permission')"))
    runtime = create_database_engine(url)
    try:
        async with runtime.begin() as connection:
            await connection.execute(
                Permission.__table__.insert().values(
                    code="test:schema:runtime", name="project permission"
                )
            )
            assert await connection.scalar(text("SELECT current_schema()")) == "enterprise_backend"
            assert (
                await connection.scalar(
                    text("SELECT name FROM permission WHERE code='test:schema:runtime'")
                )
                == "project permission"
            )
            assert (
                await connection.scalar(text("SELECT marker FROM public.permission"))
                == "foreign permission"
            )
    finally:
        await runtime.dispose()
    checked = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "check"],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


async def test_older_public_revision_cannot_move_foreign_tables(schema_database):
    url, engine = schema_database
    await legacy_database(url, engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE public.alembic_version SET version_num='e44a910b2e48'")
        )
    result = await migrate(url, "7c1d3f5a9b20")
    assert result.returncode != 0
    assert "must first be upgraded" in result.stdout + result.stderr
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('public.transport_task')"))
        assert await connection.scalar(text("SELECT to_regclass('public.user')"))
        assert await connection.scalar(text("SELECT payload FROM public.other_system")) == "keep me"


def test_postgres_offline_sql_requires_online_ownership_checks():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://unused/unused"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires online ownership checks" in result.stdout + result.stderr
    assert "CREATE TABLE" not in result.stdout
