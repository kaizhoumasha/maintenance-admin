import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import create_database_engine


@pytest.fixture
async def state_migration(request, tmp_path):
    key, backend, *initial = request.param
    if backend == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"
    else:
        url = os.getenv(key)
        if not url:
            pytest.skip(f"{key} must name a disposable migration database")
        target = make_url(url)
        main = os.getenv("TEST_DATABASE_URL")
        assert target.database and "migration" in target.database
        assert main is None or target.database != make_url(main).database
    engine = create_database_engine(url)

    async def migrate(direction, revision):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", direction, revision],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    async def schema_snapshot(prefix):
        async with engine.connect() as connection:

            def inspect_schema(sync):
                inspector = inspect(sync)
                schema = "enterprise_backend" if sync.dialect.name == "postgresql" else None

                def normalize(value):
                    if isinstance(value, dict):
                        return {key: normalize(item) for key, item in sorted(value.items())}
                    if isinstance(value, (list, tuple)):
                        return [normalize(item) for item in value]
                    if value is None or isinstance(value, (str, int, bool)):
                        return value
                    return str(value)

                return {
                    name: {
                        "columns": normalize(inspector.get_columns(name, schema=schema)),
                        "primary_key": normalize(inspector.get_pk_constraint(name, schema=schema)),
                        "unique": sorted(
                            (
                                normalize(c)
                                for c in inspector.get_unique_constraints(name, schema=schema)
                            ),
                            key=str,
                        ),
                        "indexes": sorted(
                            (normalize(i) for i in inspector.get_indexes(name, schema=schema)),
                            key=str,
                        ),
                        "checks": sorted(
                            (
                                normalize(c)
                                for c in inspector.get_check_constraints(name, schema=schema)
                            ),
                            key=str,
                        ),
                        "foreign_keys": normalize(inspector.get_foreign_keys(name, schema=schema)),
                    }
                    for name in sorted(inspector.get_table_names(schema=schema))
                    if name.startswith(prefix)
                }

            return await connection.run_sync(inspect_schema)

    try:
        if initial:
            await migrate("downgrade", "base")
        await migrate("upgrade", initial[0] if initial else "head")
        yield SimpleNamespace(
            engine=engine, migrate=migrate, backend=backend, schema_snapshot=schema_snapshot
        )
    finally:
        await engine.dispose()


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
