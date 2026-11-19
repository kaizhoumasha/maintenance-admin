from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from tests import migration_helpers

ADMIN_URL = "postgresql+asyncpg://admin:admin123@localhost:5432/history_migration_test"


def _ok():
    return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def clear_templates():
    migration_helpers._templates.clear()
    yield
    migration_helpers._templates.clear()


async def test_one_revision_is_migrated_once_and_copied(monkeypatch):
    events = []

    async def ddl(url, database, *, create):
        events.append(("ddl", database, create))

    async def alembic(url, *arguments, environment=None):
        events.append(("alembic", arguments))
        return _ok()

    async def freeze(url, database):
        events.append(("freeze", database))

    async def copy(url, database, template):
        events.append(("copy", database, template))

    monkeypatch.setattr(migration_helpers, "migration_database_ddl", ddl)
    monkeypatch.setattr(migration_helpers, "run_alembic", alembic)
    monkeypatch.setattr(migration_helpers, "_freeze_template", freeze)
    monkeypatch.setattr(migration_helpers, "_create_from_template", copy)
    async with migration_helpers.cloned_revision_database(
        ADMIN_URL, "abc123", downgrade_base=True
    ) as first:
        pass
    async with migration_helpers.cloned_revision_database(
        ADMIN_URL, "abc123", downgrade_base=True
    ) as second:
        pass
    assert make_url(first).database != make_url(second).database
    assert [event for event in events if event[0] == "alembic"] == [
        ("alembic", ("downgrade", "base")),
        ("alembic", ("upgrade", "abc123")),
    ]
    assert sum(event[0] == "copy" for event in events) == 2
    await migration_helpers.drop_migration_templates_async()
    assert ("ddl", events[-1][1], False) == events[-1]
    assert str(events[-1][1]).startswith("mig_template_")
    assert migration_helpers._templates == {}


async def test_failed_template_build_is_not_reused(monkeypatch):
    attempts = {"n": 0}

    async def ddl(url, database, *, create):
        return None

    async def alembic(url, *arguments, environment=None):
        attempts["n"] += 1
        code = 1 if attempts["n"] == 1 else 0
        return SimpleNamespace(returncode=code, stdout="failed", stderr="")

    async def freeze(url, database):
        return None

    async def copy(url, database, template):
        return None

    monkeypatch.setattr(migration_helpers, "migration_database_ddl", ddl)
    monkeypatch.setattr(migration_helpers, "run_alembic", alembic)
    monkeypatch.setattr(migration_helpers, "_freeze_template", freeze)
    monkeypatch.setattr(migration_helpers, "_create_from_template", copy)
    with pytest.raises(AssertionError, match="failed"):
        async with migration_helpers.cloned_revision_database(
            ADMIN_URL, "head", downgrade_base=False
        ):
            pass
    async with migration_helpers.cloned_revision_database(ADMIN_URL, "head", downgrade_base=False):
        pass
    assert attempts["n"] == 2


async def test_template_copy_retries_when_source_is_busy(monkeypatch):
    calls = {"create": 0}
    frozen = []

    async def autocommit(url, statement, parameters=None):
        if statement.startswith("CREATE DATABASE"):
            calls["create"] += 1
            if calls["create"] == 1:
                raise RuntimeError('source database "t" is being accessed by other users')

    async def freeze(url, database):
        frozen.append(database)

    monkeypatch.setattr(migration_helpers, "_autocommit", autocommit)
    monkeypatch.setattr(migration_helpers, "_freeze_template", freeze)
    await migration_helpers._create_from_template(ADMIN_URL, "schema_test_a", "mig_template_1_a")
    assert calls["create"] == 2
    assert frozen == ["mig_template_1_a"]
