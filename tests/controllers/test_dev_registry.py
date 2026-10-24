import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.controllers.dev import include_dev_routers
from app.integrations.ecs_callback import CallbackPayload
from app.integrations.ecs_callback import registry as ecs
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_request import operation as requests

PATH = "/api/v1/dev/integrations/registry"


async def test_registry_requires_login(client):
    assert (await client.get(PATH)).status_code == 401


async def test_registry_lists_all_three_actual_registries(client, admin, monkeypatch):
    monkeypatch.setattr(ecs, "_registry", {})

    @ecs.ecs_event("REGISTRY_PROBE", data=CallbackPayload)
    async def probe(ctx, payload):
        pass

    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200, response.text
    rows = {(row["kind"], row["name"]): row for row in response.json()}
    expected = (
        {("ecs_event", name) for name in ecs.events()}
        | {("wms_callback", name) for name in callbacks.operations()}
        | {("wms_request", name) for name in requests.operations()}
    )
    assert set(rows) == expected
    assert rows["ecs_event", "REGISTRY_PROBE"]["symbol"].endswith("probe")
    assert rows["ecs_event", "REGISTRY_PROBE"]["local_only"] is False
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


async def test_registry_remains_available_when_empty(client, admin, monkeypatch):
    for registry in (ecs, callbacks, requests):
        monkeypatch.setattr(registry, "_registry", {})
    assert (await client.get(PATH, headers=admin)).json() == []


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
    ('EXAMPLE_SCAN', registries[0]),
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


async def test_registry_describes_events_with_unvalidated_data(client, admin, monkeypatch):
    monkeypatch.setattr(ecs, "_registry", {})

    @ecs.ecs_event("RAW_DATA")
    async def raw(ctx, payload):
        pass

    response = await client.get(PATH, headers=admin)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json() if row["kind"] == "ecs_event")
    assert row["name"] == "RAW_DATA"
    assert row["payload_schema"] is None
    assert row["endpoint"] is None
