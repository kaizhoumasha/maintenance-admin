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


def test_database_name_must_be_a_plain_identifier():
    assert migration_helpers._quote_ident("mig_template_1_ab") == '"mig_template_1_ab"'
    with pytest.raises(ValueError, match="unsafe database name"):
        migration_helpers._quote_ident('bad"; drop database')


async def test_template_copy_does_not_retry_unrelated_errors(monkeypatch):
    frozen = []

    async def autocommit(url, statement, parameters=None):
        raise RuntimeError("permission denied")

    async def freeze(url, database):
        frozen.append(database)

    monkeypatch.setattr(migration_helpers, "_autocommit", autocommit)
    monkeypatch.setattr(migration_helpers, "_freeze_template", freeze)
    with pytest.raises(RuntimeError, match="permission denied"):
        await migration_helpers._create_from_template(
            ADMIN_URL, "schema_test_a", "mig_template_1_a"
        )
    assert frozen == []


async def test_template_copy_raises_after_repeated_busy_source(monkeypatch):
    frozen = []

    async def autocommit(url, statement, parameters=None):
        raise RuntimeError("other users are connected")

    async def freeze(url, database):
        frozen.append(database)

    monkeypatch.setattr(migration_helpers, "_autocommit", autocommit)
    monkeypatch.setattr(migration_helpers, "_freeze_template", freeze)
    with pytest.raises(RuntimeError, match="other users"):
        await migration_helpers._create_from_template(
            ADMIN_URL, "schema_test_a", "mig_template_1_a"
        )
    assert frozen == ["mig_template_1_a"] * 5


async def test_template_cache_key_includes_server_and_downgrade_flag(monkeypatch):
    built = []

    async def build(pg_url, name, revision, *, downgrade_base):
        built.append((migration_helpers._server_identity(pg_url), downgrade_base))

    monkeypatch.setattr(migration_helpers, "_build_revision_template", build)
    first = await migration_helpers.ensure_revision_template(
        ADMIN_URL, "head", downgrade_base=False
    )
    second = await migration_helpers.ensure_revision_template(
        ADMIN_URL, "head", downgrade_base=False
    )
    third = await migration_helpers.ensure_revision_template(ADMIN_URL, "head", downgrade_base=True)
    other = "postgresql+asyncpg://admin:secret@db.internal:5432/history_migration_test"
    fourth = await migration_helpers.ensure_revision_template(other, "head", downgrade_base=False)
    assert first == second == fourth
    assert third != first
    assert built == [
        (("localhost", 5432, "admin"), False),
        (("localhost", 5432, "admin"), True),
        (("db.internal", 5432, "admin"), False),
    ]


async def test_lost_template_race_drops_the_duplicate_and_returns_the_winner(monkeypatch):
    dropped = []

    async def ddl(url, database, *, create):
        dropped.append((database, create))

    async def build(pg_url, name, revision, *, downgrade_base):
        key = (*migration_helpers._server_identity(pg_url), revision, downgrade_base)
        migration_helpers._templates[key] = (pg_url, "winner_template")

    monkeypatch.setattr(migration_helpers, "migration_database_ddl", ddl)
    monkeypatch.setattr(migration_helpers, "_build_revision_template", build)
    winner = await migration_helpers.ensure_revision_template(
        ADMIN_URL, "rev1", downgrade_base=False
    )
    assert winner == "winner_template"
    assert len(dropped) == 1
    assert dropped[0][1] is False
    assert dropped[0][0].startswith("mig_template_")
    cached = migration_helpers._templates[
        (*migration_helpers._server_identity(ADMIN_URL), "rev1", False)
    ]
    assert cached[1] == "winner_template"


async def test_freeze_template_disconnects_and_rejects_new_sessions(monkeypatch):
    calls = []

    async def autocommit(url, statement, parameters=None):
        calls.append((statement, parameters))

    monkeypatch.setattr(migration_helpers, "_autocommit", autocommit)
    await migration_helpers._freeze_template(ADMIN_URL, "mig_template_1_ab")
    assert "pg_terminate_backend" in calls[0][0]
    assert calls[0][1] == {"name": "mig_template_1_ab"}
    assert calls[1] == (
        'ALTER DATABASE "mig_template_1_ab" WITH ALLOW_CONNECTIONS false',
        None,
    )


async def test_autocommit_disposes_the_engine_after_success_and_failure(monkeypatch):
    executed = []
    disposed = []
    seen = []

    class Connection:
        def __init__(self, fail):
            self.fail = fail

        async def execute(self, statement, parameters):
            executed.append((str(statement), parameters))
            if self.fail:
                raise RuntimeError("db down")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def __init__(self, fail):
            self.fail = fail

        def connect(self):
            return Connection(self.fail)

        async def dispose(self):
            disposed.append(True)

    def factory(url, isolation_level=None):
        seen.append((url, isolation_level))
        return Engine(fail=len(seen) > 1)

    monkeypatch.setattr(migration_helpers, "create_async_engine", factory)
    await migration_helpers._autocommit(ADMIN_URL, "SELECT 1", {"name": "db"})
    with pytest.raises(RuntimeError, match="db down"):
        await migration_helpers._autocommit(ADMIN_URL, "SELECT 1")
    assert executed == [("SELECT 1", {"name": "db"}), ("SELECT 1", {})]
    assert disposed == [True, True]
    assert seen == [(ADMIN_URL, "AUTOCOMMIT"), (ADMIN_URL, "AUTOCOMMIT")]


def test_sessionfinish_drops_cached_templates_and_ignores_an_empty_cache(monkeypatch):
    dropped = []

    async def ddl(url, database, *, create):
        dropped.append((database, create))

    monkeypatch.setattr(migration_helpers, "migration_database_ddl", ddl)
    from tests.conftest import pytest_sessionfinish

    pytest_sessionfinish(None, 0)
    assert dropped == []
    migration_helpers._templates[("localhost", 5432, "admin", "head", False)] = (
        ADMIN_URL,
        "mig_template_pid_abc",
    )
    pytest_sessionfinish(None, 0)
    assert dropped == [("mig_template_pid_abc", False)]
    assert migration_helpers._templates == {}
