import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.controllers.dev import include_dev_routers
from app.integrations.ecs_callback import CallbackPayload
from app.integrations.ecs_callback import registry as ecs
from app.integrations.ecs_status import query
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_request import operation as requests

PATH = "/api/v1/dev/integrations/registry"


async def test_registry_requires_login(client):
    assert (await client.get(PATH)).status_code == 401


async def test_registry_lists_all_three_actual_registries(client, admin, monkeypatch):
    monkeypatch.setattr(ecs, "_registry", {})

    @ecs.ecs_event("REGISTRY_PROBE", data=CallbackPayload, device_role="SCAN1")
    async def probe(ctx, payload):
        pass

    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200, response.text
    rows = {
        (
            row["kind"],
            (row["device_role"], row["name"]) if row["kind"] == "ecs_event" else row["name"],
        ): row
        for row in response.json()
    }
    expected = (
        {("ecs_event", name) for name in ecs.events()}
        | {("wms_callback", name) for name in callbacks.operations()}
        | {("wms_request", name) for name in requests.operations()}
    )
    assert {
        key for key, row in rows.items() if row["entry_type"] == "business_registration"
    } == expected
    assert set(rows) == expected | {("ecs_status", "ecs.device.status@v1")}
    builtin = rows["ecs_status", "ecs.device.status@v1"]
    assert builtin["entry_type"] == "builtin_capability"
    assert builtin["module"] == query.__module__
    assert builtin["symbol"] == query.__qualname__
    assert builtin["local_only"] is False
    assert builtin["method"] == "GET"
    assert builtin["path"] == "/api/v1/device/status"
    assert builtin["payload_schema"] is None
    assert builtin["endpoint"] is None
    assert "devices" in builtin["response_schema"]["properties"]
    assert "ecs_origin" in builtin["query_schema"]["properties"]
    assert rows["ecs_event", ("SCAN1", "REGISTRY_PROBE")]["symbol"].endswith("probe")
    assert rows["ecs_event", ("SCAN1", "REGISTRY_PROBE")]["local_only"] is False
    for key, row in rows.items():
        assert row["source_file"].endswith(".py")
        assert row["module"]
        if key[0] == "wms_request":
            assert row["endpoint"] == requests.lookup(key[1]).endpoint.value
            assert row["payload_schema"]["properties"]
    for kind, name in [
        ("wms_callback", "example.echo@v1"),
        ("wms_request", "example.fact.demo@v1"),
    ]:
        assert rows[kind, name]["local_only"] is True
    assert rows["wms_callback", "transport.task.resulted@v1"]["local_only"] is False


async def test_builtin_remains_when_business_registries_are_empty(client, admin, monkeypatch):
    for registry in (ecs, callbacks, requests):
        monkeypatch.setattr(registry, "_registry", {})
    rows = (await client.get(PATH, headers=admin)).json()
    assert [(row["kind"], row["entry_type"]) for row in rows] == [
        ("ecs_status", "builtin_capability")
    ]


async def test_catalog_does_not_query_ecs(client, admin, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("catalog must not perform any outbound query")

    monkeypatch.setattr("app.http_client.AsyncHttpClient.send", forbidden)
    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200
    assert sum(row["kind"] == "ecs_status" for row in response.json()) == 1


@pytest.mark.parametrize("environment", ["production", "test"])
def test_registry_is_not_mounted_outside_local(environment):
    app = FastAPI()
    include_dev_routers(app, environment=environment)
    assert PATH not in app.openapi()["paths"]


@pytest.mark.parametrize("environment", ["local", "production"])
def test_fresh_process_only_loads_demo_registrations_in_local(environment):
    script = """
import os
from app.main import app
from app.integrations.ecs_callback.registry import events
from app.integrations.wms_callback.registry import operations as callbacks
from app.integrations.wms_request.operation import operations as requests

local = os.environ['ENVIRONMENT'] == 'local'
assert ('/api/v1/dev/integrations/registry' in app.openapi()['paths']) is local
registries = [events(), callbacks(), requests()]
examples = [
    (('EXAMPLE_SCAN', 'EXAMPLE_SCAN'), registries[0]),
    ('example.echo@v1', registries[1]),
    ('example.fact.demo@v1', registries[2]),
]
for name, registry in examples:
    assert (name in registry) is local
    if local:
        assert registry[name].registration_source.local_only is True
for registry in registries:
    for item in registry.values():
        if not local:
            assert item.registration_source.local_only is False
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={
            **os.environ,
            "ENVIRONMENT": environment,
            "DATABASE_URL": "sqlite+aiosqlite://",
            "RELIABLE_TARGETS": "{}",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_registry_describes_each_event_role_and_payload(client, admin, monkeypatch):
    monkeypatch.setattr(ecs, "_registry", {})

    @ecs.ecs_event("RAW_DATA", device_role="SCAN1", data=CallbackPayload)
    async def raw(ctx, payload):
        pass

    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json() if row["kind"] == "ecs_event")
    assert row["name"] == "RAW_DATA"
    assert row["device_role"] == "SCAN1"
    assert row["payload_schema"]["type"] == "object"
    assert row["endpoint"] is None


@pytest.mark.parametrize("roles", [("SCAN1",), ("SCAN1", "SCAN4")])
async def test_catalog_reads_role_command_contracts_from_the_configured_routes(
    client, admin, monkeypatch, roles
):
    from types import SimpleNamespace

    from app.integrations.ecs_command import (
        RoleRoutes,
        compile_commands,
        ecs_command,
    )
    from app.integrations.ecs_command.contracts import ErrorDetail
    from app.main import app
    from tests.helpers import TrialPayload

    class Move(TrialPayload):
        pass

    ecs_command(
        "MOVE",
        device_roles=roles,
        annex_id="trial",
        annex_version="1",
        result=TrialPayload,
        error=ErrorDetail,
        timeout_min_ms=1,
        timeout_max_ms=60000,
    )(Move)
    bindings, annexes = compile_commands(Move)

    async def forbidden(*args):
        raise AssertionError("catalog must not resolve runtime devices")

    routes = RoleRoutes(
        bindings,
        annexes=annexes,
        resolve_binding=forbidden,
        resolve_route=forbidden,
    )
    monkeypatch.setattr(
        app.state, "ecs_commands", SimpleNamespace(routes=routes, annexes=annexes), raising=False
    )
    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200
    entries = [row for row in response.json() if row["kind"] == "ecs_command"]
    assert {(row["device_role"], row["name"]) for row in entries} == {
        (role, "MOVE") for role in roles
    }
    assert all(row["entry_type"] == "command_contract" for row in entries)
    assert all(row["symbol"].endswith("Move") for row in entries)
    assert len(entries) == len(roles)
    assert all(row["module"] == __name__ for row in entries)
    assert all(row["source_file"].endswith("test_dev_registry.py") for row in entries)
    assert all(row["local_only"] is False for row in entries)
    assert all("qty" in row["payload_schema"]["properties"] for row in entries)


def test_formal_event_registration_requires_an_injected_binding_resolver(monkeypatch):
    from app.controllers.ecs_callback import include_ecs_callback

    monkeypatch.setattr(ecs, "_registry", {})

    @ecs.ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=CallbackPayload)
    async def scan(ctx, payload):
        pass

    with pytest.raises(ValueError, match="binding resolver"):
        include_ecs_callback(FastAPI(), environment="production")
