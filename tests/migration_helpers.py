import os
import shutil
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import create_database_engine
from tests.helpers import run_alembic


async def add_http_runtime_test_schema(engine):
    """Provide current ORM columns while preserving the revision under test."""
    from importlib import import_module

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def expand(connection):
        schema = "enterprise_backend" if connection.dialect.name == "postgresql" else None
        context = MigrationContext.configure(connection)
        with Operations.context(context) as operations:
            for name, kind in (
                ("frozen_request_wire", sa.LargeBinary()),
                ("frozen_request_sha256", sa.String(64)),
            ):
                operations.add_column("reliable_message", sa.Column(name, kind), schema=schema)
            for revision in ("http_exchange_expand", "http_exchange_runtime_expand"):
                migration = import_module(f"migrations.versions.2026-11-13_{revision}")
                migration.upgrade()

    async with engine.begin() as connection:
        await connection.run_sync(expand)


@pytest.fixture(scope="session")
def sqlite_migration_start(tmp_path_factory):
    templates = {}

    async def copy(revision, destination):
        if revision not in templates:
            template = tmp_path_factory.mktemp("migration-start") / "state.db"
            result = await run_alembic(f"sqlite+aiosqlite:///{template}", "upgrade", revision)
            assert result.returncode == 0, result.stdout + result.stderr
            templates[revision] = template
        shutil.copyfile(templates[revision], destination)

    return copy


@pytest.fixture
async def state_migration(request, tmp_path, sqlite_migration_start):
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
    async with AsyncExitStack() as databases:
        if backend != "sqlite":
            # Keep prior cases' catalog maintenance out of this source's writer-drain proof.
            url, _ = await databases.enter_async_context(isolated_schema_database(url))
        engine = create_database_engine(url)

        async def migrate(direction, revision):
            result = await run_alembic(url, direction, revision)
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
                            "primary_key": normalize(
                                inspector.get_pk_constraint(name, schema=schema)
                            ),
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
                            "foreign_keys": normalize(
                                inspector.get_foreign_keys(name, schema=schema)
                            ),
                        }
                        for name in sorted(inspector.get_table_names(schema=schema))
                        if name.startswith(prefix)
                    }

                return await connection.run_sync(inspect_schema)

        try:
            if backend == "sqlite" and initial:
                await sqlite_migration_start(initial[0], tmp_path / "state.db")
            else:
                if initial:
                    await migrate("downgrade", "base")
                await migrate("upgrade", initial[0] if initial else "head")
            yield SimpleNamespace(
                engine=engine, migrate=migrate, backend=backend, schema_snapshot=schema_snapshot
            )
        finally:
            await engine.dispose()


async def migration_database_ddl(pg_url, database, *, create):
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            statement = (
                f'CREATE DATABASE "{database}"'
                if create
                else f'DROP DATABASE "{database}" WITH (FORCE)'
            )
            await connection.execute(text(statement))
    finally:
        await admin.dispose()


@asynccontextmanager
async def isolated_schema_database(pg_url):
    database = "schema_test_" + uuid4().hex
    await migration_database_ddl(pg_url, database, create=True)
    url = make_url(pg_url).set(database=database).render_as_string(hide_password=False)
    engine = create_async_engine(url)
    try:
        yield url, engine
    finally:
        await engine.dispose()
        await migration_database_ddl(pg_url, database, create=False)


@pytest.fixture
async def schema_database(pg_url):
    async with isolated_schema_database(pg_url) as database:
        yield database


async def source_table(connection, name):
    schema = "enterprise_backend" if connection.dialect.name == "postgresql" else None
    return await connection.run_sync(
        lambda c: Table(name, MetaData(), schema=schema, autoload_with=c, resolve_fks=False)
    )


async def seed_transport_identity_case(factory, messages):
    import json

    from app.integrations.wms_transport.types import RackPosition, TransportCaller
    from app.integrations.wms_transport.wire import freeze_request, input_digest, move_rack_data

    now = datetime(2026, 11, 7, tzinfo=UTC)
    operation_id = "00000000-0000-7000-8000-000000000711"
    task = dict(
        id=101,
        client_request_id="original-client",
        transport_task_id="original-transport",
        operation_id=operation_id,
        kind="RACK_MOVE",
        input_digest="b" * 64,
        workline_id="original-line",
        station_id=None,
        owner_domain="coarse_sorting",
        cycle_id=None,
        created_at=now,
        frozen=move_rack_data("R", RackPosition("A"), RackPosition("B")),
    )
    task["input_digest"] = input_digest(
        "move_rack", TransportCaller("original-line", None), task["frozen"]
    )
    raw = freeze_request(
        operation_id, task["transport_task_id"], int(now.timestamp() * 1000), task["frozen"]
    )
    async with factory.begin() as session:
        connection = await session.connection()
        transports = await source_table(connection, "transport_task")
        outbox = await source_table(connection, "reliable_message")
        await session.execute(transports.insert().values(**task))
        for identity, target, operation, business_key in messages:
            envelope = json.loads(raw)
            envelope["operation"] = operation
            envelope["data"]["transport_task_id"] = business_key
            request = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
            await session.execute(
                outbox.insert().values(
                    id=identity,
                    target=target,
                    operation=operation,
                    operation_id=operation_id,
                    business_key=business_key,
                    request_raw=request,
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=now,
                    not_received_count=0,
                    delivery_uncertain=False,
                    created_at=now,
                    updated_at=now,
                )
            )
    return task


async def seed_command_message(factory, *, mismatch=False):
    NOW = datetime(2026, 11, 7, tzinfo=UTC)
    from app.core.strict_json import canonical_digest

    command_code = "00000000-0000-7000-8000-000000000099"
    core = {
        "command_code": command_code,
        "device_code": "D1",
        "result": "SUCCESS",
        "finish_time": 1,
        "data": {},
        "error_detail": None,
    }
    digest = canonical_digest(core)
    async with factory.begin() as session:
        c = await session.connection()
        command = await source_table(c, "ecs_command")
        message = await source_table(c, "reliable_message")
        result = await source_table(c, "ecs_result_evidence")
        send = await source_table(c, "ecs_send_evidence")
        await session.execute(
            command.insert().values(
                command_code=command_code,
                owner_domain="coarse_sorting",
                call_key="original-call",
                call_key_digest="b" * 64,
                input_raw=b"original-input",
                device_code="D1",
                task_type="MOVE",
                route={"annex_id": "old", "annex_version": "1"},
                request_raw=b"original-wire",
                target="ecs",
                delivery_snapshot={"protocol": "ecs-command"},
                expires_at=NOW,
                status="SUCCEEDED",
                delivery_status="DONE",
                blocked=False,
                needs_reconciliation=False,
                applied_result_id=digest,
                created_at=NOW,
            )
        )
        await session.execute(
            message.insert().values(
                id=71,
                target="ecs",
                operation="ecs.command",
                operation_id=command_code,
                delivery_snapshot={"protocol": "ecs-command"},
                request_raw=b"different-wire" if mismatch else b"original-wire",
                response_raw=b"ack",
                status="DONE",
                attempts=1,
                next_attempt_at=NOW,
                expires_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.execute(
            result.insert().values(
                id=81,
                command_code=command_code,
                digest=digest,
                core=core,
                status_code=200,
                response={"code": 200, "message": "ACK"},
                applied=True,
                created_at=NOW,
            )
        )
        await session.execute(
            send.insert().values(
                id=91,
                command_code=command_code,
                lease_id="original-lease",
                outcome="NOT_RECEIVED",
                response_raw=b"reject",
                http_status=503,
                late=True,
                created_at=NOW,
            )
        )
    return command_code, digest


def legacy_callback_tables():
    from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, Integer, String, Text

    from app.database import SCHEMA

    metadata = MetaData(schema=SCHEMA)
    receipt = Table(
        "callback_receipt",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("operation", String(256)),
        Column("operation_id", String(64)),
        Column("raw_digest", String(64)),
        Column("effective_digest", String(64)),
        Column("compare_mode", String(9)),
        Column("status_code", Integer),
        Column("body", JSON),
        Column("resp_timestamp", BigInteger),
        Column("created_at", DateTime(timezone=True)),
    )
    snapshot = Table(
        "callback_snapshot",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("request_id", Text),
        Column("received_at", DateTime(timezone=True)),
        Column("body_b64", Text),
        Column("truncated", Boolean),
        Column("http_status", Integer),
        Column("response_body", JSON),
        Column("outcome", String(32)),
        Column("source", String(16)),
    )
    return receipt, snapshot
