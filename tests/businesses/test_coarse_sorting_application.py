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

    facts = {"barcode", "reel_diameter", "reel_thickness"}
    schema = ScanCompletedData.model_json_schema()
    assert set(schema["properties"]) == facts
    assert all(set(example) == facts for example in schema["examples"])


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
            "reel_diameter": 15,
            "reel_thickness": 20,
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
    assert original["payload"] == e.body["data"]
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
