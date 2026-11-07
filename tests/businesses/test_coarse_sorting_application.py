"""The deployed catalog uses the same coarse scan entry in every environment."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from app.integrations import history as history_records
from tests.helpers import seed_transport_task

SCAN_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/versions/2026-10-29_coarse_sorting_scan.py"
)
WMS_TARGETS = json.dumps(
    {
        "targets": {
            protocol: {"protocol": protocol, "base_url": "http://127.0.0.1:1"}
            for protocol in ("wms-decisions", "wms-facts", "wms-transport")
        }
    }
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


@pytest.mark.parametrize(
    "environment,tail", [("production", False), ("local", False), ("local", True)]
)
def test_api_and_worker_start_with_the_same_coarse_contracts(environment, tail):
    code = """
import json
from app.businesses import catalog
from app.integrations.wms_request.operation import operations

bindings, _ = catalog.validate_business_contracts()
print(json.dumps({
    "commands": sorted(
        [binding.device_role, binding.task_type, binding.annex_id, binding.annex_version]
        for binding in bindings
        if "coarse_sorting" in catalog.contract_consumers(
            ("ecs_command", binding.task_type, binding.device_role)
        )
    ),
    "requests": sorted(
        [name, item.endpoint.value] for name, item in operations().items()
        if "coarse_sorting" in catalog.contract_consumers(("wms_request", name, None))
    ),
}))
"""
    reports = []
    for module in ("app.main", "app.tasks.celery_app"):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}\n{code}"],
            env={
                **os.environ,
                "ENVIRONMENT": environment,
                "COARSE_SORTING_TAIL_ENABLED": str(tail).lower(),
                "DATABASE_URL": "sqlite+aiosqlite://",
                "RELIABLE_TARGETS": WMS_TARGETS,
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(json.loads(result.stdout.splitlines()[-1]))
    assert reports[0] == reports[1]
    command_names = {tuple(command[:2]) for command in reports[0]["commands"]}
    requests = dict(reports[0]["requests"])
    expected_commands = {("CONVEYOR", "MOVE_FORWARD")}
    expected_requests = {"inbound.coarse_sorting.validate_bind@v1": "decisions"}
    if tail:
        expected_commands.update({("INPUT_ARM", "MOVE_TO_NG"), ("OUTPUT_ARM", "PUT_TO_BIN")})
        expected_requests.update(
            {
                "inbound.coarse_sorting.storage_allocate@v1": "decisions",
                "inbound.coarse_sorting.execution_report@v1": "facts",
                "rack.departure_decide@v1": "decisions",
            }
        )
    assert command_names == expected_commands
    assert requests == expected_requests


@pytest.mark.parametrize("module", ["app.main", "app.tasks.celery_app"])
@pytest.mark.parametrize("invalid_target", ["missing", "snapshot"])
def test_coarse_integration_cannot_start_without_usable_wms_targets(module, invalid_target):
    config = json.loads(WMS_TARGETS)
    if invalid_target == "missing":
        del config["targets"]["wms-decisions"]
    else:
        config["targets"]["wms-decisions"] = {
            "protocol": "wms-decisions",
            "address_mode": "SNAPSHOT",
        }
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env={
            **os.environ,
            "ENVIRONMENT": "local",
            "COARSE_SORTING_TAIL_ENABLED": "true",
            "RELIABLE_TARGETS": json.dumps(config),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires one STATIC target for wms-decisions" in result.stderr


def test_worker_rejects_malformed_coarse_locations_at_startup():
    result = subprocess.run(
        [sys.executable, "-c", "import app.tasks.celery_app"],
        env={
            **os.environ,
            "ENVIRONMENT": "local",
            "COARSE_SORTING_TAIL_ENABLED": "true",
            "COARSE_SORTING_NG_LOCATIONS": '{"COARSE":" "}',
            "RELIABLE_TARGETS": WMS_TARGETS,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "COARSE_SORTING_NG_LOCATIONS must map workline codes to valid locations" in result.stderr


def test_coarse_tail_cannot_be_enabled_in_production():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="available only in the local integration mode"):
        Settings(ENVIRONMENT="production", COARSE_SORTING_TAIL_ENABLED=True)


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
    outbox = Outbox({}, {}, records=history_records)
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

        from datetime import UTC, datetime

        from sqlalchemy import delete

        from app.core.identifiers import uuid7
        from app.models import TransportTask

        task_id = "transport-" + str(uuid7())
        async with e.factory.begin() as session:
            await seed_transport_task(
                session,
                TransportTask(
                    client_request_id=str(uuid7()),
                    transport_task_id=task_id,
                    operation_id=str(uuid7()),
                    kind="RACK_MOVE",
                    input_digest="a" * 64,
                    workline_id=str(e.lines[0]),
                    owner_domain="coarse_sorting",
                    created_at=datetime.now(UTC),
                    frozen={
                        "kind": "RACK_MOVE",
                        "rack_id": "R-WAKE",
                        "source": {"kind": "RACK", "location_code": "R-WAKE"},
                        "target": {"kind": "RACK_POSITION", "location_code": "KT-WAKE"},
                        "target_face": "90",
                        "rcs_template_id": "F01",
                    },
                ),
            )
        try:
            applied = await e.client.post(
                "/api/v1/wms/events",
                json={
                    "operation_id": str(uuid7()),
                    "operation": "transport.task.resulted@v1",
                    "timestamp": 1,
                    "data": {
                        "transport_task_id": task_id,
                        "kind": "RACK_MOVE",
                        "outcome_revision": 1,
                        "rack_id": "R-WAKE",
                        "status": "SUCCEEDED",
                        "final_position": {"kind": "RACK_POSITION", "location_code": "KT-WAKE"},
                    },
                },
            )
            assert applied.status_code == 202
            assert published == ["coarse_sorting", "coarse_sorting"]
        finally:
            async with e.factory.begin() as session:
                await session.execute(
                    delete(TransportTask).where(TransportTask.transport_task_id == task_id)
                )


async def test_scan_projection_survives_receipt_retention_in_configured_database(configured_scan):

    from app.models import IntegrationExchange

    e = configured_scan
    url = "/api/v1/dev/ecs/events"
    first = await e.client.post(url, json=e.body, headers=e.headers)
    digest = first.json()["digest"]
    original = (await e.client.get(url + "/" + digest, headers=e.headers)).json()["business"]
    async with e.factory.begin() as session:
        await session.execute(
            update(IntegrationExchange)
            .where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True)
            )
            .values(receipt_active=False)
            .where(IntegrationExchange.operation_id == digest)
        )
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
    job_migration = runpy.run_path(
        str(SCAN_MIGRATION.with_name("2026-11-06_coarse_sorting_job.py"))
    )
    migration = runpy.run_path(str(SCAN_MIGRATION))

    ng_migration = runpy.run_path(str(SCAN_MIGRATION.with_name("2026-11-06_coarse_sorting_ng.py")))
    rack_migration = runpy.run_path(
        str(SCAN_MIGRATION.with_name("2026-11-06_coarse_rack_preparation.py"))
    )

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            rack_migration["downgrade"]()
            ng_migration["downgrade"]()
            job_migration["downgrade"]()
            migration["downgrade"]()

    with pytest.raises(CommandError, match="coarse scan records exist"):
        async with e.factory.kw["bind"].begin() as connection:
            await connection.run_sync(downgrade)
    assert (await e.client.get(url + "/" + digest, headers=e.headers)).json()[
        "business"
    ] == original
