"""The deployed catalog uses the same coarse scan entry in every environment."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCAN_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/versions/2026-10-29_coarse_sorting_scan.py"
)


def test_scan_contract_exposes_only_device_facts():
    from app.businesses.coarse_sorting.contracts import ScanCompletedData

    facts = {"barcode", "size_inch", "thickness_mm"}
    schema = ScanCompletedData.model_json_schema()
    assert set(schema["properties"]) == facts
    for example in schema["examples"]:
        assert ScanCompletedData.model_validate(example).model_dump() == {
            key: example[key] for key in facts
        }


@pytest.mark.parametrize("environment", ["local", "production"])
def test_main_uses_catalog_owned_coarse_scan_in_every_environment(environment):
    code = """
from app.main import app
from app.businesses import catalog
from app.integrations.ecs_callback.registry import lookup
codes = {d.code for d in catalog.definitions()}
assert 'coarse_sorting' in codes, 'coarse scan is not in the deployed catalog'
handler=lookup('INPUT_ARM','SCAN_COMPLETED')
assert handler is not None
expected = 'app.businesses.coarse_sorting.ecs_event.scan_completed'
assert handler.registration_source.module == expected
assert not handler.registration_source.local_only
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "ENVIRONMENT": environment,
            "DATABASE_URL": "sqlite+aiosqlite://",
            "RELIABLE_TARGETS": "{}",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def configured_scan(client, admin, callback_db, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import delete

    from app.businesses import catalog
    from app.businesses.coarse_sorting.models import CoarseSortingScan
    from app.businesses.runtime import build_runtime
    from app.core.identifiers import uuid7
    from app.integrations.ecs_command import EcsCommands
    from app.main import app
    from app.models import Device, DeviceRoute, WorkLine
    from app.reliable import Outbox
    from app.services.device_routes import DeviceRoutes
    from tests.helpers import add_bound_device

    bindings, annexes = catalog.validate_business_contracts()
    outbox = Outbox({}, {})
    commands = EcsCommands(outbox, "coarse-test", DeviceRoutes(bindings, annexes=annexes), annexes)
    runtime = build_runtime(outbox=outbox, ecs_commands=commands, session_factory=callback_db)
    monkeypatch.setattr(app.state, "business_runtime", runtime, raising=False)
    lines, codes, routes, conveyor_codes = [], [], [], []
    async with callback_db.begin() as session:
        for _ in range(2):
            code = "SCAN_" + uuid7().hex
            line, device, route = await add_bound_device(
                session, code, "http://127.0.0.1:1", role="INPUT_ARM"
            )
            conveyor_code = "CONVEYOR_" + uuid7().hex
            conveyor_route = DeviceRoute(
                id=str(uuid7()), device_code=conveyor_code, ecs_origin="http://127.0.0.1:1"
            )
            session.add(conveyor_route)
            await session.flush()
            session.add(
                Device(
                    device_code=conveyor_code,
                    display_name=conveyor_code,
                    current_route_id=conveyor_route.id,
                    work_line_id=line.id,
                )
            )
            line.device_bindings = {"INPUT_ARM": code, "CONVEYOR": conveyor_code}
            conveyor_codes.append(conveyor_code)
            routes.append(conveyor_route.id)
            line.business_code = "coarse_sorting"
            line.position_bindings = {"OUTPUT_RACK": "RACK_" + code}
            lines.append(line.id)
            codes.append(code)
            routes.append(route.id)
    body = {
        "device_code": codes[0],
        "event_type": "SCAN_COMPLETED",
        "timestamp": 1793206799000,
        "data": {
            "barcode": " RAW,TEST-1 ",
            "size_inch": 7,
            "thickness_mm": 15.3,
        },
    }
    try:
        yield SimpleNamespace(
            client=client, headers=admin, factory=callback_db, body=body, codes=codes, lines=lines
        )
    finally:
        async with callback_db.begin() as session:
            await session.execute(
                delete(CoarseSortingScan).where(CoarseSortingScan.device_code.in_(codes))
            )
            await session.execute(
                delete(Device).where(Device.device_code.in_([*codes, *conveyor_codes]))
            )
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(lines)))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id.in_(routes)))


async def test_debug_and_public_callback_share_configured_scan_and_projection(configured_scan):
    e = configured_scan
    url = "/api/v1/dev/ecs/events"
    delivered = await e.client.post(url, json=e.body, headers=e.headers)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["outcome"] == "received"
    digest = delivered.json()["digest"]
    state = await e.client.get(url + "/" + digest, headers=e.headers)
    assert state.status_code == 200, state.text
    original = state.json()["business"]
    assert original["payload"] == {
        "barcode": e.body["data"]["barcode"],
        "reel_diameter": 7,
        "reel_thickness": 15.3,
    }
    assert original["pending"] is True
    assert original["work_line_id"] == e.lines[0]
    repeated = await e.client.post("/api/v1/callback/event", json=e.body)
    assert repeated.status_code == 200
    assert (await e.client.get(url + "/" + digest, headers=e.headers)).json()[
        "business"
    ] == original
    other = await e.client.post(url, json={**e.body, "device_code": e.codes[1]}, headers=e.headers)
    other_state = (await e.client.get(url + "/" + other.json()["digest"], headers=e.headers)).json()
    assert other_state["business"]["work_line_id"] == e.lines[1]
    assert other_state["business"]["id"] != original["id"]


async def test_main_lifespan_enables_committed_scan_wakeup(configured_scan, monkeypatch):
    from contextlib import asynccontextmanager

    from redis.asyncio import Redis
    from sqlalchemy import select

    import app.main as main
    from app.businesses.coarse_sorting.models import CoarseSortingScan
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import EcsCommandAdapter

    e = configured_scan
    commands = main.app.state.business_runtime.services["coarse_sorting"].capabilities.ecs
    published = []
    original_lifespan = main.reliable_lifespan
    database_url = e.factory.kw["bind"].url.render_as_string(hide_password=False)

    @asynccontextmanager
    async def isolated_sender(*args, **kwargs):
        async with original_lifespan(*args, **kwargs, database_url=database_url) as outbox:
            yield outbox

    redis_client = Redis.from_url(os.environ["TEST_REDIS_URL"])

    def redis_factory():
        return redis_client

    redis_factory.cache_clear = lambda: None
    monkeypatch.setattr(main, "get_redis", redis_factory)
    monkeypatch.setattr(main, "engine", e.factory.kw["bind"])
    monkeypatch.setattr(main, "session_factory", e.factory)
    monkeypatch.setattr(main, "reliable_lifespan", isolated_sender)
    from app import business_wakeups

    monkeypatch.setattr(business_wakeups, "enqueue_business", published.append)
    monkeypatch.setattr(main.app.state, "business_runtime", None)
    monkeypatch.setattr(main.app.state, "ecs_commands", commands, raising=False)
    monkeypatch.setattr(main.app.state, "reliable_adapters", {"ecs-command": EcsCommandAdapter()})
    monkeypatch.setattr(
        main.app.state,
        "reliable_lifecycles",
        {"ecs-command": EcsLifecycle(commands)},
        raising=False,
    )
    monkeypatch.setattr(main.app.state, "reliable_client_provider", DynamicClients(), raising=False)
    async with main.lifespan(main.app):
        response = await e.client.post("/api/v1/callback/event", json=e.body)
        assert response.status_code == 200
        async with e.factory() as reader:
            scan = await reader.scalar(
                select(CoarseSortingScan).where(CoarseSortingScan.device_code == e.codes[0])
            )
            assert published == ["coarse_sorting"]
            assert scan.pending and scan.validation_message_id is None


async def test_scan_projection_survives_receipt_retention_in_configured_database(configured_scan):
    from sqlalchemy import delete

    from app.models import CallbackReceipt

    e = configured_scan
    url = "/api/v1/dev/ecs/events"
    first = await e.client.post(url, json=e.body, headers=e.headers)
    digest = first.json()["digest"]
    original = (await e.client.get(url + "/" + digest, headers=e.headers)).json()["business"]
    async with e.factory.begin() as session:
        await session.execute(delete(CallbackReceipt).where(CallbackReceipt.operation_id == digest))
    state = (await e.client.get(url + "/" + digest, headers=e.headers)).json()
    assert state["receipt"] is None
    assert state["business"] == original
    assert (await e.client.post(url, json=e.body, headers=e.headers)).json()[
        "outcome"
    ] == "received"
    assert (await e.client.get(url + "/" + digest, headers=e.headers)).json()[
        "business"
    ] == original


async def test_schema_downgrade_preserves_accepted_scan(configured_scan):
    import runpy

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.util import CommandError

    e = configured_scan
    url = "/api/v1/dev/ecs/events"
    delivered = await e.client.post(url, json=e.body, headers=e.headers)
    digest = delivered.json()["digest"]
    original = (await e.client.get(url + "/" + digest, headers=e.headers)).json()["business"]
    migration = runpy.run_path(str(SCAN_MIGRATION))

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()

    with pytest.raises(CommandError, match="coarse scan records exist"):
        async with e.factory.kw["bind"].begin() as connection:
            await connection.run_sync(downgrade)
    assert (await e.client.get(url + "/" + digest, headers=e.headers)).json()[
        "business"
    ] == original
