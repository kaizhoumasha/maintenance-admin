import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine
from tests.helpers import run_alembic
from tests.migration_helpers import (  # noqa: F401
    migration_database_ddl,
    schema_database,
    sqlite_migration_start,
    state_migration,
)


@pytest.fixture(scope="module")
def pg_url(pg_url):
    database = "schema_test_" + uuid4().hex
    url = make_url(pg_url).set(database=database).render_as_string(hide_password=False)
    # Dynamic legacy fixtures request this while their own event loop is already running.
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(asyncio.run, migration_database_ddl(pg_url, database, create=True)).result()
        try:
            yield url
        finally:
            worker.submit(
                asyncio.run, migration_database_ddl(pg_url, database, create=False)
            ).result()


@pytest.fixture
async def pg_sessions(pg_url):
    initialized = await run_alembic(pg_url, "upgrade", "head")
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    engine = create_database_engine(pg_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
