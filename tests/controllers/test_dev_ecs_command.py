from datetime import timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ConfigDict, create_model
from sqlalchemy import func, select

from app.core import get_current_user
from app.main import app
from app.models import EcsCommand, ReliableMessage
from app.reliable import Outbox
from tests.helpers import reset_ecs_assembly

BODY = {
    "client_request_id": "demo-1",
    "ecs_url": "http://127.0.0.1:8000",
    "device_code": "D1",
    "task_type": "MOVE",
    "timeout_ms": 30000,
    "params": {"qty": 1},
}


@pytest.fixture
async def dev_ecs(admin, monkeypatch):
    from app.integrations.ecs_command.dev import configure_local_ecs

    reset_ecs_assembly(app, monkeypatch)
    configure_local_ecs(
        app,
        environment="local",
    )
    from app.integrations.ecs_command.runtime import ecs_runtime_config
    from app.main import settings

    service = app.state.ecs_commands
    service.outbox = Outbox(
        ecs_runtime_config(settings, service).targets,
        app.state.reliable_adapters,
        lifecycles=app.state.reliable_lifecycles,
    )
    monkeypatch.setattr(app.state, "outbox", service.outbox, raising=False)
    monkeypatch.setattr(
        app.state, "callback_session_factory", app.state.audit_session_factory, raising=False
    )
    return admin


async def test_debug_create_requires_login_and_explicit_assembly(client, admin):
    answer = await client.post("/api/v1/dev/ecs/commands", json=BODY)
    assert answer.status_code == 401
    answer = await client.post("/api/v1/dev/ecs/commands", headers=admin, json=BODY)
    assert answer.status_code == 503
    assert "lifespan" in answer.json()["message"]


async def test_debug_creation_replays_original_identity_and_scopes_reads(client, dev_ecs):
    first = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    assert first.status_code == 200, first.text
    code = first.json()["command_code"]
    again = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    assert again.json()["command_code"] == code
    conflict = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "params": {"qty": 2}}
    )
    assert conflict.status_code == 409
    answer = await client.get(f"/api/v1/dev/ecs/commands/{code}", headers=dev_ecs)
    assert answer.status_code == 200 and answer.json()["result"] is None
    async with app.state.audit_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
    original = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: type("User", (), {"id": 999})()
    try:
        assert (await client.get(f"/api/v1/dev/ecs/commands/{code}")).status_code == 404
    finally:
        if original is None:
            app.dependency_overrides.pop(get_current_user)
        else:
            app.dependency_overrides[get_current_user] = original


@pytest.mark.parametrize(
    "extra",
    [
        {"owner": "other"},
        {"call_key": "x"},
        {"ecs_origin": "http://example.com"},
        {"state": {"is_online": True, "status": "IDLE"}},
    ],
)
async def test_debug_body_cannot_supply_authority_or_destination(client, dev_ecs, extra):
    assert (
        await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, **extra})
    ).status_code == 400


@pytest.mark.parametrize("request_id", [None, "", 1])
async def test_debug_create_requires_stable_nonempty_request_id(client, dev_ecs, request_id):
    assert (
        await client.post(
            "/api/v1/dev/ecs/commands",
            headers=dev_ecs,
            json={**BODY, "client_request_id": request_id},
        )
    ).status_code == 400


async def test_mock_rejects_nonloopback_callers_and_only_acknowledges_valid_commands():
    from app.controllers.dev_ecs_command import mock_router
    from app.core import register_exception_handlers

    target = FastAPI()
    register_exception_handlers(target)
    target.include_router(mock_router, prefix="/api/v1")
    envelope = {
        "device_code": "D1",
        "task_type": "MOVE",
        "command_code": "c1",
        "priority": 1,
        "timeout": 30000,
        "timestamp": 1,
        "params": {"qty": 1},
    }
    for host, expected in (("192.0.2.1", 403), ("127.0.0.1", 200)):
        async with AsyncClient(
            transport=ASGITransport(app=target, client=(host, 1)), base_url="http://test"
        ) as peer:
            answer = await peer.post("/api/v1/device/command", json=envelope)
            assert answer.status_code == expected
            if expected == 200:
                assert answer.json() == {"code": 200, "message": "ACK"}
                assert (
                    await peer.post(
                        "/api/v1/device/command", json={**envelope, "params": {"qty": "wrong"}}
                    )
                ).status_code == 400


async def test_debug_commit_failure_leaves_no_command_or_obligation(client, dev_ecs):
    from sqlalchemy.exc import OperationalError

    from app.core.database import get_session

    original = app.dependency_overrides[get_session]

    async def failing_session():
        async with app.state.audit_session_factory() as session:

            async def fail_commit():
                raise OperationalError("commit", {}, Exception("sensitive-value"))

            session.commit = fail_commit
            yield session

    app.dependency_overrides[get_session] = failing_session
    try:
        answer = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
        assert answer.status_code == 503
        assert "sensitive-value" not in answer.text
    finally:
        app.dependency_overrides[get_session] = original
    async with app.state.audit_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_debug_manual_result_preserves_early_result_lock(client, dev_ecs):
    created = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    code = created.json()["command_code"]
    response = await client.post(
        "/api/v1/callback/result",
        json={
            "command_code": code,
            "device_code": "D1",
            "result": "SUCCESS",
            "finish_time": 1,
            "data": {"qty": 1},
        },
    )
    assert response.status_code == 409
    view = (await client.get(f"/api/v1/dev/ecs/commands/{code}", headers=dev_ecs)).json()
    assert view["hold_reason"] == "EARLY_RESULT_LOCK" and view["result"] is None
    replay = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    assert replay.json()["command_code"] == code
    assert replay.json()["hold_reason"] == "EARLY_RESULT_LOCK"


def test_swagger_documents_complete_command_examples():
    schema = app.openapi()
    create = schema["paths"]["/api/v1/dev/ecs/commands"]["post"]
    request_ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][request_ref.rsplit("/", 1)[1]]
    assert model["examples"][0] == BODY
    for operation in (create, schema["paths"]["/api/v1/dev/ecs/commands/{command_code}"]["get"]):
        example = operation["responses"]["200"]["content"]["application/json"]["example"]
        assert example["command_code"] and example.get("result") is None
        assert not example["ack_received"]

    view = schema["components"]["schemas"]["DevCommandView"]
    assert "result" in view["required"]
    assert {"type": "null"} in view["properties"]["result"]["anyOf"]


def test_swagger_offers_platform_and_effective_business_command_requests():
    create = app.openapi()["paths"]["/api/v1/dev/ecs/commands"]["post"]
    examples = create["requestBody"]["content"]["application/json"]["examples"]
    assert examples["platform:MOVE"]["value"] == BODY
    assert examples["CONVEYOR:MOVE_FORWARD"]["value"] == {
        "client_request_id": "demo-CONVEYOR-MOVE_FORWARD-1",
        "ecs_url": "http://127.0.0.1:8000",
        "device_code": "CONVEYOR-01",
        "task_type": "MOVE_FORWARD",
        "timeout_ms": 30000,
        "params": {"barcode": "RAW-001"},
    }
    assert "占位" in examples["CONVEYOR:MOVE_FORWARD"]["description"]
    assert create["operationId"] == "create_command_api_v1_dev_ecs_commands_post"
    query = app.openapi()["paths"]["/api/v1/dev/ecs/commands/{command_code}"]["get"]
    assert query["operationId"] == "get_command_api_v1_dev_ecs_commands__command_code__get"


async def test_selected_business_example_keeps_generic_debug_snapshot_and_identity(client, dev_ecs):
    import json

    create = app.openapi()["paths"]["/api/v1/dev/ecs/commands"]["post"]
    examples = create["requestBody"]["content"]["application/json"]["examples"]
    body = examples["CONVEYOR:MOVE_FORWARD"]["value"]
    first = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=body)
    assert first.status_code == 200, first.text
    code = first.json()["command_code"]
    repeated = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=body)
    assert repeated.json()["command_code"] == code
    conflict = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**body, "params": {"barcode": "OTHER"}}
    )
    assert conflict.status_code == 409
    async with app.state.audit_session_factory() as session:
        row = await session.get(EcsCommand, code)
        assert json.loads(row.request_raw)["params"] == {"barcode": "RAW-001"}
        assert row.route["version"] == "3"
        assert (row.route["annex_id"], row.route["annex_version"]) == ("ecs.dev.protocol", "1")
        assert row.route["retry_window"] == 300
    # Picking an example does not switch the free-form endpoint to its strict business model.
    generic = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**body, "client_request_id": "generic-move-forward", "params": {"opaque": [1, None]}},
    )
    assert generic.status_code == 200, generic.text


@pytest.mark.parametrize(
    "samples,timeout_range,params,timeout",
    [
        (None, (1, 20), {}, 20),
        ([None, "not an object", []], (40000, 50000), {}, 40000),
        (
            [None, {"barcode": "ALIAS-01"}, {"barcode": "ALIAS-02"}],
            (0, 60000),
            {"barcode": "ALIAS-01"},
            30000,
        ),
    ],
)
def test_mounted_examples_use_effective_bindings_model_samples_and_timeout_bounds(
    samples, timeout_range, params, timeout
):
    from app.controllers.dev import include_dev_routers
    from app.integrations.ecs_command import compile_commands, ecs_command
    from app.integrations.ecs_command.contracts import ErrorDetail

    config = ConfigDict(strict=True, extra="forbid")
    if samples is not None:
        config["json_schema_extra"] = {"examples": samples}
    model = create_model("SharedMove", __config__=config, barcode=(str, ...))
    ecs_command(
        "MOVE_SHARED",
        device_roles=("ARM_A", "ARM_B", "INACTIVE"),
        annex_id="test.shared",
        annex_version="1",
        result=model,
        error=ErrorDetail,
        timeout_min_ms=timeout_range[0],
        timeout_max_ms=timeout_range[1],
    )(model)
    bindings, _ = compile_commands(model)
    effective = tuple(binding for binding in bindings if binding.device_role != "INACTIVE")
    target = FastAPI()
    include_dev_routers(target, environment="local", command_bindings=effective)
    document = target.openapi()
    media = document["paths"]["/api/v1/dev/ecs/commands"]["post"]["requestBody"]["content"][
        "application/json"
    ]
    examples = media["examples"]
    assert set(examples) == {"platform:MOVE", "ARM_A:MOVE_SHARED", "ARM_B:MOVE_SHARED"}
    identities = set()
    for role in ("ARM_A", "ARM_B"):
        sample = examples[f"{role}:MOVE_SHARED"]
        value = sample["value"]
        assert sample["summary"] == f"{role} / MOVE_SHARED"
        assert value["params"] == params
        assert value["timeout_ms"] == timeout
        assert set(value) == set(BODY)
        identities.add(value["client_request_id"])
        if not params:
            assert "params" in sample["description"] and "注册目录" in sample["description"]
    assert len(identities) == 2
    schema = document["components"]["schemas"]["DevCommandIn"]
    assert set(schema["properties"]) == set(BODY)
    assert "enum" not in schema["properties"]["task_type"]

    # A second mount cannot leak its examples into the first application's snapshot.
    other = FastAPI()
    include_dev_routers(other, environment="local", command_bindings=())
    other_examples = other.openapi()["paths"]["/api/v1/dev/ecs/commands"]["post"]["requestBody"][
        "content"
    ]["application/json"]["examples"]
    assert set(other_examples) == {"platform:MOVE"}
    assert set(
        target.openapi()["paths"]["/api/v1/dev/ecs/commands"]["post"]["requestBody"]["content"][
            "application/json"
        ]["examples"]
    ) == set(examples)


async def test_requests_select_dynamic_origins_without_device_registration(client, dev_ecs):
    created = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    assert created.status_code == 200, created.text
    assert created.json()["ecs_url"] == BODY["ecs_url"]
    denied = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**BODY, "client_request_id": "new", "ecs_url": "http://127.0.0.1:9000"},
    )
    assert denied.status_code == 200
    async with app.state.audit_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 2
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 2


def test_swagger_requires_request_origin():
    schema = app.openapi()["components"]["schemas"]["DevCommandIn"]
    assert "ecs_url" in schema["required"]


@pytest.mark.parametrize(
    "url",
    [
        None,
        1,
        "",
        "http://localhost:8000",
        "http://example.com",
        "ftp://127.0.0.1",
        "http://0.0.0.0",
        "http://[::]",
        "http://224.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:70000",
        "http://127.0.0.1/path",
        "http://USER:PASSWORD@127.0.0.1",
        "http://127.0.0.1?token=secret",
        "http://127.0.0.1/#x",
    ],
)
async def test_debug_rejects_invalid_origin_without_obligation(client, dev_ecs, url):
    reply = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "ecs_url": url}
    )
    assert reply.status_code == 400, reply.text
    assert "password" not in reply.text.lower() and "token=secret" not in reply.text
    async with app.state.audit_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_debug_requires_origin_and_server_owned_authority(client, dev_ecs):
    missing = {k: v for k, v in BODY.items() if k != "ecs_url"}
    assert (
        await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=missing)
    ).status_code == 400
    for key, value in (
        ("kind", "mock"),
        ("annex_id", "test"),
        ("mode", "mock"),
        ("is_mock", True),
    ):
        assert (
            await client.post(
                "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, key: value}
            )
        ).status_code == 400


@pytest.mark.parametrize("version", ["1", "2"])
async def test_missing_protocol_policy_only_blocks_new_registration(
    client, dev_ecs, monkeypatch, version
):
    from app.integrations.ecs_command.dev import DevEcsRoutes

    a = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    code = a.json()["command_code"]
    async with app.state.audit_session_factory.begin() as session:
        row = await session.get(EcsCommand, code)
        row.route = {**row.route, "version": version}
    from dataclasses import replace
    from datetime import timedelta

    resolve = DevEcsRoutes.resolve

    async def invalid_window(routes, *args):
        return replace(await resolve(routes, *args), retry_window=timedelta(seconds=60))

    monkeypatch.setattr(DevEcsRoutes, "resolve", invalid_window)
    for url, expected in ((BODY["ecs_url"] + "/", 200), ("http://127.0.0.1:9000", 409)):
        answer = await client.post(
            "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "ecs_url": url}
        )
        assert answer.status_code == expected
        if expected == 200:
            assert answer.json()["command_code"] == code
            assert answer.json()["ecs_url"] == BODY["ecs_url"]
    denied = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "client_request_id": "new"}
    )
    assert denied.status_code == 503
    view = await client.get(f"/api/v1/dev/ecs/commands/{code}", headers=dev_ecs)
    assert view.json()["ecs_url"] == BODY["ecs_url"]


@pytest.mark.parametrize("permitted", [False, True])
@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://192.0.2.1:9000"])
async def test_network_registration_requires_permission_and_replay_survives_revocation(
    client, dev_ecs, permitted, origin
):
    from sqlalchemy import delete

    from app.models import Role, role_permission
    from tests.helpers import bearer, make_user_with_perms

    user = await make_user_with_perms(client, dev_ecs, ["dev:ecs:send_real"] if permitted else [])
    body = {**BODY, "ecs_url": origin, "device_code": "TRIAL_DEVICE", "task_type": "TRIAL_MOVE"}
    result = await client.post("/api/v1/dev/ecs/commands", headers=bearer(user), json=body)
    assert result.status_code == (200 if permitted else 403), result.text
    async with app.state.audit_session_factory.begin() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == permitted
        if permitted:
            role_id = await session.scalar(select(Role.id).where(Role.name == "r"))
            await session.execute(
                delete(role_permission).where(role_permission.c.role_id == role_id)
            )
    if permitted:
        replay = await client.post("/api/v1/dev/ecs/commands", headers=bearer(user), json=body)
        assert replay.json()["command_code"] == result.json()["command_code"]
        denied = await client.post(
            "/api/v1/dev/ecs/commands",
            headers=bearer(user),
            json={**body, "client_request_id": "new"},
        )
        assert denied.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        {"params": []},
        {"timeout_ms": True},
        {"timeout_ms": -1},
        {"timeout_ms": 2**63},
        {"task_type": 1},
        {"device_code": False},
    ],
)
async def test_debug_enforces_protocol_types(client, dev_ecs, change):
    reply = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, **change})
    assert reply.status_code == 400
    async with app.state.audit_session_factory() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_equivalent_origin_uses_persisted_normalized_address(client, dev_ecs, monkeypatch):
    from app.integrations.ecs_command.dev import DevEcsRoutes

    monkeypatch.setattr(
        app.state.ecs_commands,
        "routes",
        DevEcsRoutes(),
    )
    first = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**BODY, "ecs_url": "HTTP://127.0.0.1:80/"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["ecs_url"] == "http://127.0.0.1"
    again = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "ecs_url": "http://127.0.0.1"}
    )
    assert again.json()["command_code"] == first.json()["command_code"]


async def test_equivalent_ipv6_origin_replays_identity(client, dev_ecs, monkeypatch):
    from app.integrations.ecs_command.dev import DevEcsRoutes

    service = app.state.ecs_commands
    monkeypatch.setattr(
        service,
        "routes",
        DevEcsRoutes(),
    )
    created = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**BODY, "ecs_url": "http://[0:0:0:0:0:0:0:1]:8000/"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["ecs_url"] == "http://[::1]:8000"
    replay = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "ecs_url": "http://[::1]:8000"}
    )
    assert (
        replay.status_code == 200
        and replay.json()["command_code"] == created.json()["command_code"]
    )


@pytest.mark.parametrize(
    "action,params",
    [
        ("LIFT", {"level": 2, "nested": [True, None, {"x": "y"}]}),
        ("SCAN", {"label": "a", "qty": "opaque"}),
    ],
)
async def test_debug_accepts_unregistered_actions_with_generic_json(
    client, dev_ecs, action, params
):
    import json

    answer = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**BODY, "task_type": action, "params": params, "timeout_ms": 0},
    )
    assert answer.status_code == 200, answer.text
    async with app.state.audit_session_factory() as session:
        row = await session.get(EcsCommand, answer.json()["command_code"])
        assert json.loads(row.request_raw)["params"] == params
        assert row.route["version"] == "3"
        assert row.route["retry_window"] == 300


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", '{"nested":[NaN]}'])
async def test_debug_rejects_nonfinite_json_without_obligation(client, dev_ecs, value):
    import json

    raw = json.dumps(BODY).replace('"qty": 1', '"qty": ' + value)
    answer = await client.post(
        "/api/v1/dev/ecs/commands",
        headers={**dev_ecs, "Content-Type": "application/json"},
        content=raw,
    )
    assert answer.status_code == 400
    async with app.state.audit_session_factory() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_debug_rejects_oversized_frozen_envelope(client, dev_ecs):
    answer = await client.post(
        "/api/v1/dev/ecs/commands",
        headers=dev_ecs,
        json={**BODY, "params": {"blob": "a" * 262144}},
    )
    assert answer.status_code == 400
    async with app.state.audit_session_factory() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("window", [None, 0, 300, True, "300", timedelta(0), timedelta(seconds=-1)])
async def test_missing_or_malformed_protocol_policy_is_configuration_error(
    client, dev_ecs, monkeypatch, window
):
    import app.integrations.ecs_command.dev as dev

    first = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    monkeypatch.setattr(dev, "PROTOCOL_RETRY_WINDOW", window)
    replay = await client.post("/api/v1/dev/ecs/commands", headers=dev_ecs, json=BODY)
    assert replay.json()["command_code"] == first.json()["command_code"]
    denied = await client.post(
        "/api/v1/dev/ecs/commands", headers=dev_ecs, json={**BODY, "client_request_id": "new"}
    )
    assert denied.status_code == 503
    assert "PROTOCOL_RETRY_WINDOW" in denied.json()["message"]
    async with app.state.audit_session_factory() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
