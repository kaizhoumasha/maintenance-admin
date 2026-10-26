import pytest
from fastapi import FastAPI

from tests.helpers import (
    ecs_history_annexes,
    ecs_protocol_annexes,
    reset_ecs_assembly,
    simulator_sender,
)


def test_local_assembly_preserves_other_protocols_and_fails_on_conflict():
    from app.integrations.ecs_command.dev import configure_local_ecs

    app = FastAPI()
    wms = object()
    app.state.reliable_adapters = {"wms-transport": wms}
    configure_local_ecs(app, environment="local")
    assert app.state.reliable_adapters["wms-transport"] is wms
    original = app.state.ecs_commands
    with pytest.raises(ValueError, match="conflict"):
        configure_local_ecs(app, environment="local")
    assert app.state.ecs_commands is original


@pytest.mark.parametrize("environment", ["production", "staging", "test"])
@pytest.mark.parametrize(
    "debug", [{"history_annexes": ecs_history_annexes()}, {"client_factory": object()}]
)
def test_nonlocal_rejects_debug_authorization(environment, debug):
    from app.integrations.ecs_command.dev import configure_local_ecs

    app = FastAPI()
    with pytest.raises(ValueError, match="local"):
        configure_local_ecs(app, environment=environment, **debug)
    assert not hasattr(app.state, "ecs_commands")


async def test_current_registration_policy_does_not_rewrite_frozen_window(monkeypatch):
    from dataclasses import asdict
    from datetime import timedelta

    import app.integrations.ecs_command.dev as dev
    from app.integrations.ecs_command.routing import RouteAuthorization

    routes = dev.DevEcsRoutes()
    frozen = asdict(await routes.for_origin("http://127.0.0.1:8000").resolve(None, "D1", "MOVE"))
    frozen["retry_window"] = 300
    monkeypatch.setattr(dev, "PROTOCOL_RETRY_WINDOW", timedelta(seconds=12))
    assert await routes.authorize_frozen(None, frozen) == RouteAuthorization.ALLOW
    current = await routes.for_origin("http://127.0.0.1:9000").resolve(None, "D2", "LIFT")
    assert current.retry_window == timedelta(seconds=12)
    assert frozen["origin"].endswith(":8000") and frozen["retry_window"] == 300


def test_nonlocal_application_does_not_mount_debug_or_mock_routes():
    from app.controllers.dev import include_dev_routers

    app = FastAPI()
    include_dev_routers(app, environment="production")
    paths = {route.path for route in app.routes}
    assert "/api/v1/dev/ecs/commands" not in paths
    assert "/api/v1/device/command" not in paths
    assert not hasattr(app.state, "ecs_commands")


@pytest.mark.parametrize("device_result", ["SUCCESS", "FAILED"])
async def test_main_lifespan_assembles_ecs_once_with_restricted_factory(
    ecs,
    loopback_server,
    monkeypatch,
    real_redis,
    device_result,
):
    import asyncio

    import httpx
    from sqlalchemy import delete, select

    import app.main as main
    import app.reliable_runtime as runtime
    from app.core import hash_password
    from app.core.database import get_session
    from app.core.identifiers import uuid7
    from app.core.redis import get_redis
    from app.models import Permission, ReliableMessage, Role, User
    from tests.ecs_swagger import create_app

    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("Application Sender uses an independent PostgreSQL connection")
    app = main.app
    senders = []
    original_sender = runtime.Sender

    def sender(*args, **kwargs):
        instance = original_sender(*args, **kwargs)
        senders.append(instance)
        return instance

    monkeypatch.setattr(runtime, "Sender", sender)
    assemblies = []
    configure = main.configure_local_ecs

    def observe_assembly(application, **kwargs):
        assert getattr(application.state, "ecs_commands", None) is None
        assert getattr(application.state, "reliable_client_provider", None) is None
        assert "client_factory" in kwargs
        result = configure(application, **kwargs)
        assemblies.append(result)
        return result

    monkeypatch.setattr(main, "configure_local_ecs", observe_assembly)
    username = "ecs_" + uuid7().hex
    async with ecs.factory.begin() as session:
        permission = await session.scalar(
            select(Permission).where(Permission.code == "dev:ecs:send_real")
        )
        owns_permission = permission is None
        if owns_permission:
            permission = Permission(code="dev:ecs:send_real", name="Network commissioning")
        role = Role(name=username, permissions=[permission])
        user = User(
            username=username,
            email=username + "@example.test",
            hashed_password=hash_password("password123"),
            roles=[role],
        )
        session.add(user)
    overrides = app.dependency_overrides.copy()

    async def sessions():
        async with ecs.factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_redis] = lambda: real_redis
    reset_ecs_assembly(app, monkeypatch)
    monkeypatch.setattr(app.state, "callback_session_factory", ecs.factory, raising=False)
    try:
        async with loopback_server(app) as origin:
            settings = main.settings.model_copy(
                update={
                    "DATABASE_URL": str(ecs.engine.url.render_as_string(hide_password=False)),
                    "RELIABLE_TARGETS": {
                        "targets": {
                            "wms": {
                                "protocol": "wms-transport",
                                "base_url": origin,
                            }
                        }
                    },
                }
            )
            monkeypatch.setattr(main, "settings", settings)
            monkeypatch.setattr(runtime, "get_settings", lambda: settings)
            monkeypatch.setattr(app.router, "lifespan_context", app.router.lifespan_context)
            assert create_app() is app
            assert getattr(app.state, "ecs_commands", None) is None
            assert getattr(app.state, "reliable_client_provider", None) is None
            from httpcore._backends.auto import AutoBackend

            connections = []
            connect_tcp = AutoBackend.connect_tcp

            async def observe_tcp(self, host, port, **kwargs):
                connections.append((host, port))
                assert port != 1, "unowned TCP connection attempted"
                return await connect_tcp(self, host, port, **kwargs)

            monkeypatch.setattr(AutoBackend, "connect_tcp", observe_tcp)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(base_url=origin, trust_env=False) as client,
            ):
                schema = (await client.get("/openapi.json")).json()
                assert "/api/v1/dev/ecs/commands" in schema["paths"]
                assert "/api/v1/device/command" in schema["paths"]
                login = await client.post(
                    "/api/v1/auth/login", json={"username": username, "password": "password123"}
                )
                assert login.status_code == 200, login.text
                headers = {"Authorization": "Bearer " + login.json()["access_token"]}
                body = {
                    "client_request_id": username,
                    "ecs_url": app.state.ecs_simulator_origins[0],
                    "device_code": "D1",
                    "task_type": "MOVE",
                    "timeout_ms": 30000,
                    "params": {"qty": 1},
                }
                created = await client.post("/api/v1/dev/ecs/commands", headers=headers, json=body)
                assert created.status_code == 200, created.text
                code = created.json()["command_code"]
                assert len(senders) == len(assemblies) == 1
                assert assemblies[0] is app.state.ecs_commands
                with pytest.raises(ValueError, match="conflict"):
                    configure(app, environment="local")
                assert app.state.ecs_commands is assemblies[0]
                assert app.state.ecs_commands.outbox is app.state.outbox
                assert app.state.outbox.targets["wms"].base_url == origin
                assert len(app.state.outbox.targets) == 2

                async def wait_for_ack(command_code):
                    async with asyncio.timeout(15):
                        while True:
                            state = await client.get(
                                f"/api/v1/dev/ecs/commands/{command_code}", headers=headers
                            )
                            assert state.status_code == 200, state.text
                            if state.json()["ack_received"]:
                                return state
                            await asyncio.sleep(0.5)

                state = await wait_for_ack(code)
                assert state.json()["status"] == "WAITING_RESULT"
                assert state.json()["result"] is None
                second = await client.post(
                    "/api/v1/dev/ecs/commands",
                    headers=headers,
                    json={
                        **body,
                        "client_request_id": username + "-second",
                        "device_code": "D2",
                        "task_type": "LIFT",
                        "params": {"level": 2, "nested": [None, True, {"label": "sample"}]},
                        "ecs_url": app.state.ecs_simulator_origins[1],
                    },
                )
                assert second.status_code == 200, second.text
                await wait_for_ack(second.json()["command_code"])
                assert {hit["device_code"] for hit in app.state.ecs_simulator_hits} == {"D1", "D2"}
                blocked = await client.post(
                    "/api/v1/dev/ecs/commands",
                    headers=headers,
                    json={
                        **body,
                        "client_request_id": username + "-blocked",
                        "ecs_url": "http://127.0.0.1:1",
                    },
                )
                assert blocked.status_code == 200, blocked.text
                async with asyncio.timeout(15):
                    while True:
                        async with ecs.factory() as session:
                            pending = await session.scalar(
                                select(ReliableMessage).where(
                                    ReliableMessage.operation_id == blocked.json()["command_code"]
                                )
                            )
                            if pending.attempts and pending.status == "PENDING":
                                assert not pending.delivery_uncertain
                                break
                        await asyncio.sleep(0.5)
                assert not any(port == 1 for _, port in connections)
                assert len(app.state.ecs_simulator_hits) == 2
                assert {hit["task_type"] for hit in app.state.ecs_simulator_hits} == {
                    "MOVE",
                    "LIFT",
                }
                from sqlalchemy import func

                from app.models import EcsCommand, EcsSendEvidence

                async with ecs.factory() as session:
                    for sent_code, raw in app.state.ecs_simulator_raw.items():
                        assert (await session.get(EcsCommand, sent_code)).request_raw == raw
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(EcsSendEvidence)
                            .where(EcsSendEvidence.command_code == blocked.json()["command_code"])
                        )
                        == 0
                    )
                result_body = {
                    "command_code": code,
                    "device_code": "D1",
                    "result": device_result,
                    "finish_time": 1,
                    "data": {"measurements": [1, {"unit": "mm"}], "ok": True},
                }
                if device_result == "FAILED":
                    result_body["error_detail"] = {"code": "TEST_FAILURE", "msg": "test failure"}
                for _ in range(2):
                    response = await client.post("/api/v1/callback/result", json=result_body)
                    assert response.status_code == 200, response.text
                final = await client.get(f"/api/v1/dev/ecs/commands/{code}", headers=headers)
                assert final.json()["result"]["result"] == device_result
                replay = await client.post("/api/v1/dev/ecs/commands", headers=headers, json=body)
                assert replay.json()["command_code"] == code
    finally:
        from app.services.session_service import revoke_all

        await revoke_all(real_redis, str(user.id))
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        async with ecs.factory.begin() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.execute(delete(Role).where(Role.id == role.id))
            if owns_permission:
                await session.execute(delete(Permission).where(Permission.id == permission.id))
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == "ecs-local")
            )


@pytest.mark.parametrize("same_origin", [True, False])
async def test_debug_concurrent_identity_uses_final_winner_origin(
    ecs, monkeypatch, same_origin, loopback_server
):
    import asyncio

    from sqlalchemy import func, select

    from app.core.errors import AppError, ErrorCode
    from app.integrations.ecs_command.dev import DevEcsRoutes, create_debug_command
    from app.models import EcsCommand, ReliableMessage

    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL unique-key arbitration")
    from fastapi import Request

    hits = []

    def peer(label):
        target = FastAPI()

        @target.post("/api/v1/device/command")
        async def acknowledge(request: Request):
            hits.append((label, await request.body()))
            return {"code": 200, "message": "ACK"}

        return target

    async with loopback_server(peer("A")) as a, loopback_server(peer("B")) as b:
        ecs.service.annexes = ecs_protocol_annexes()
        ecs.service.routes = DevEcsRoutes()
        ready = asyncio.Event()
        entered = 0
        resolve = DevEcsRoutes.resolve

        async def simultaneous(routes, *args):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            async with asyncio.timeout(5):
                await ready.wait()
            return await resolve(routes, *args)

        monkeypatch.setattr(DevEcsRoutes, "resolve", simultaneous)

        async def authorize_send():
            pass

        async def create(origin):
            try:
                async with ecs.factory.begin() as session:
                    view, frozen = await create_debug_command(
                        session,
                        service=ecs.service,
                        owner=ecs.owner,
                        client_request_id="race",
                        ecs_url=origin,
                        device_code="D1",
                        task_type="MOVE",
                        timeout_ms=30000,
                        params={"qty": 1},
                        authorize_send=authorize_send,
                    )
                    return view.command_code, frozen
            except AppError as error:
                assert error.code == ErrorCode.CONFLICT
                return None

        async with asyncio.timeout(10):
            results = await asyncio.gather(create(a), create(a if same_origin else b))
        winners = [r for r in results if r is not None]
        assert len(winners) == (2 if same_origin else 1)
        assert len(set(winners)) == 1
        async with ecs.factory() as session:
            assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
            assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
            row = await session.scalar(select(EcsCommand))
            assert row.route["origin"] == winners[0][1]
            assert row.delivery_snapshot["origin"] == winners[0][1]

        async with simulator_sender(ecs, [a, b]) as sender:
            assert await sender.tick() == 1
            async with asyncio.timeout(10):
                await asyncio.gather(*list(sender.inflight))
            assert len(hits) == 1
            assert hits[0][0] == ("A" if winners[0][1] == a else "B")
            assert hits[0][1] == row.request_raw


@pytest.mark.parametrize(
    "change", ["v1", "v2", "old_grant", "missing_annex", "unknown_policy", "extra_fields"]
)
async def test_sender_fences_unavailable_policy_without_unholding(ecs, change):
    from app.integrations.ecs_command.dev import DevEcsRoutes
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.models import EcsCommand
    from app.reliable.store import MessageStore

    ecs.service.annexes = ecs_protocol_annexes()
    ecs.service.routes = DevEcsRoutes().for_origin("http://127.0.0.1:8000")
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="gate",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
        row = await session.get(EcsCommand, command.command_code)
        if change in ("v1", "v2"):
            row.route = {**row.route, "version": change[1:]}
        if change == "old_grant":
            row.route = {**row.route, "route_id": "dev.ecs.grant"}
        if change == "missing_annex":
            row.route = {**row.route, "annex_id": "missing"}
        if change == "unknown_policy":
            row.route = {**row.route, "route_id": "unrecognized-policy"}
        if change == "extra_fields":
            row.route = {**row.route, "kind": "mock"}
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    ecs.service.routes = DevEcsRoutes()
    assert not await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory() as session:
        row = await session.get(EcsCommand, command.command_code)
        assert row.delivery_status == "HELD"
        original, frozen = row.request_raw, dict(row.route)
    ecs.service.routes = DevEcsRoutes()
    assert not await store.claim(1, ecs.targets)
    async with ecs.factory() as session:
        row = await session.get(EcsCommand, command.command_code)
        assert row.delivery_status == "HELD" and row.request_raw == original and row.route == frozen


async def test_request_contexts_send_frozen_bytes_to_independent_simulators(ecs, loopback_server):
    import asyncio

    from fastapi import Request

    from app.integrations.ecs_command.dev import DevEcsRoutes, create_debug_command
    from app.models import EcsCommand

    hits = {}

    def peer(label):
        target = FastAPI()

        @target.post("/api/v1/device/command")
        async def acknowledge(request: Request):
            hits[label] = await request.body()
            return {"code": 200, "message": "ACK"}

        return target

    async def authorize_send():
        pass

    async with loopback_server(peer("A")) as a, loopback_server(peer("B")) as b:
        ecs.service.annexes = ecs_protocol_annexes()
        ecs.service.routes = DevEcsRoutes()
        commands = {}
        for key, origin in (("A", a), ("B", b)):
            async with ecs.factory.begin() as session:
                view, frozen = await create_debug_command(
                    session,
                    service=ecs.service,
                    owner=ecs.owner,
                    client_request_id=key,
                    ecs_url=origin,
                    device_code="D1",
                    task_type="MOVE",
                    timeout_ms=30000,
                    params={"qty": 1},
                    authorize_send=authorize_send,
                )
                assert frozen == origin
                commands[key] = view.command_code
        async with simulator_sender(ecs, [a, b]) as sender:
            assert await sender.tick() == 2
            async with asyncio.timeout(10):
                await asyncio.gather(*list(sender.inflight))
            assert set(hits) == {"A", "B"}
            async with ecs.factory() as session:
                for key, code in commands.items():
                    row = await session.get(EcsCommand, code)
                    assert hits[key] == row.request_raw
                    assert row.delivery_status == "DONE" and row.status == "WAITING_RESULT"


@pytest.fixture
def runtime_settings():
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://127.0.0.1:1/0",
        JWT_SECRET_KEY="test-secret-test-secret-test-secret-32",
        RELIABLE_DEFAULT_TIMEOUT_MS=12000,
        RELIABLE_TARGETS={
            "defaults": {"max_body_bytes": 2048, "retry_base_s": 2},
            "targets": {
                "wms": {
                    "protocol": "wms-transport",
                    "base_url": "http://127.0.0.1:9000",
                    "timeout_ms": 17000,
                }
            },
        },
    )


def test_automatic_channel_preserves_wms_targets_and_resource_defaults(runtime_settings):
    from types import SimpleNamespace

    from app.integrations.ecs_command.runtime import ecs_runtime_config

    config = ecs_runtime_config(runtime_settings, SimpleNamespace(target="ecs-local"))
    wms = config.targets["wms"]
    ecs = config.targets["ecs-local"]
    assert wms.base_url == "http://127.0.0.1:9000" and wms.timeout_ms == 17000
    assert ecs.base_url is None and ecs.protocol == "ecs-command" and ecs.address_mode == "SNAPSHOT"
    assert ecs.timeout_ms == 12000 and ecs.max_body_bytes == 2048 and ecs.retry_base_s == 2
    assert config.max_concurrency == runtime_settings.RELIABLE_MAX_CONCURRENCY
    assert set(runtime_settings.RELIABLE_TARGETS["targets"]) == {"wms"}


async def test_formal_route_provider_uses_automatic_channel_without_debug_defaults(ecs):
    from sqlalchemy import select

    from app.core.config import Settings
    from app.integrations.ecs_command import configure_ecs
    from app.integrations.ecs_command.runtime import ecs_runtime_config
    from app.models import EcsCommand, ReliableMessage
    from app.reliable import Outbox

    app = FastAPI()
    service = configure_ecs(app, routes=ecs.routes, annexes=ecs.service.annexes)
    settings = Settings(
        _env_file=None,
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://127.0.0.1:1/0",
        JWT_SECRET_KEY="test-secret-test-secret-test-secret-32",
        ENVIRONMENT="production",
    )
    config = ecs_runtime_config(settings, service)
    service.outbox = Outbox(
        config.targets, app.state.reliable_adapters, lifecycles=app.state.reliable_lifecycles
    )
    for device, origin in (
        ("DEVICE_A", "http://127.0.0.1:8000"),
        ("DEVICE_B", "http://127.0.0.1:9000"),
    ):
        ecs.routes.origin = origin
        async with ecs.factory.begin() as session:
            command = await service.create(
                session,
                owner=ecs.owner,
                call_key=device,
                device_code=device,
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )
            row = await session.get(EcsCommand, command.command_code)
            assert row.route["origin"] == origin and row.device_code == device
    async with ecs.factory() as session:
        commands = (await session.scalars(select(EcsCommand))).all()
        messages = (await session.scalars(select(ReliableMessage))).all()
        assert len(commands) == len(messages) == 2
        assert len({row.target for row in commands}) == 1
        assert {row.device_code: row.route["origin"] for row in commands} == {
            "DEVICE_A": "http://127.0.0.1:8000",
            "DEVICE_B": "http://127.0.0.1:9000",
        }
    assert not any(route.path.startswith("/api/v1/dev/") for route in app.routes)


def test_automatic_channel_never_overwrites_an_existing_protocol_target(runtime_settings):
    from types import SimpleNamespace

    from app.integrations.ecs_command.runtime import ecs_runtime_config

    with pytest.raises(ValueError, match="conflict"):
        ecs_runtime_config(runtime_settings, SimpleNamespace(target="wms"))
    assert runtime_settings.RELIABLE_TARGETS["targets"]["wms"]["protocol"] == "wms-transport"


@pytest.mark.parametrize("destination", ["http://127.0.0.1:1", "http://192.0.2.1:9000"])
async def test_isolated_sender_rejects_unowned_origin_before_tcp(ecs, monkeypatch, destination):
    import asyncio

    from httpcore._backends.auto import AutoBackend
    from sqlalchemy import func, select

    from app.integrations.ecs_command.dev import DevEcsRoutes
    from app.models import EcsCommand, EcsSendEvidence

    connections = []

    async def forbidden_tcp(*args, **kwargs):
        connections.append((args, kwargs))
        raise AssertionError("No TCP connection may start for an unowned destination")

    monkeypatch.setattr(AutoBackend, "connect_tcp", forbidden_tcp)
    ecs.service.annexes = ecs_protocol_annexes()
    ecs.service.routes = DevEcsRoutes().for_origin(destination)
    async with ecs.factory.begin() as session:
        view = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="unowned",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
    async with simulator_sender(ecs, ["http://127.0.0.1:8000"]) as sender:
        assert await sender.tick() == 1
        async with asyncio.timeout(5):
            await asyncio.gather(*list(sender.inflight))
        assert connections == []
        async with ecs.factory() as session:
            row = await session.get(EcsCommand, view.command_code)
            assert row.route["origin"] == destination and row.delivery_status == "PENDING"
            assert row.hold_reason is None
            assert await session.scalar(select(func.count()).select_from(EcsSendEvidence)) == 0


@pytest.mark.parametrize(
    "history",
    [
        {("ecs.dev.protocol", "1"): next(iter(ecs_history_annexes().values()))},
        {("", "1"): next(iter(ecs_history_annexes().values()))},
        {"trial": next(iter(ecs_history_annexes().values()))},
        {("trial", "1"): object()},
    ],
)
def test_history_annexes_cannot_override_builtin_contract(history):
    from app.integrations.ecs_command.dev import configure_local_ecs

    app = FastAPI()
    wms = object()
    app.state.reliable_adapters = {"wms-transport": wms}
    original = dict(history)
    with pytest.raises(ValueError):
        configure_local_ecs(app, environment="local", history_annexes=history)
    assert history == original
    assert not hasattr(app.state, "ecs_commands")
    assert app.state.reliable_adapters == {"wms-transport": wms}


@pytest.mark.parametrize("outcome", ["ACK", "UNKNOWN"])
async def test_startup_merges_history_annexes_for_original_results(ecs, monkeypatch, outcome):
    from datetime import UTC, datetime, timedelta

    import httpx
    from sqlalchemy import func, select

    import app.main as main
    import app.reliable_runtime as runtime
    from app.models import EcsCommand, EcsResultEvidence, EcsSendEvidence, ReliableMessage
    from tests.ecs_swagger import create_app

    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("Application Sender uses an independent PostgreSQL connection")
    codes = []
    for key in ("history-applied", "history-unprocessed"):
        async with ecs.factory.begin() as session:
            view = await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key=key,
                device_code="D1",
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )
            row = await session.get(EcsCommand, view.command_code)
            # Durable pre-upgrade fixture; no old dev policy is ever dispatched.
            row.route = {
                **row.route,
                "route_id": "dev.ecs.network",
                "version": "2",
                "annex_id": "trial",
                "annex_version": "1",
                "retry_window": 60,
                "origin": "http://127.0.0.1:1",
            }
            row.delivery_snapshot = {**row.delivery_snapshot, "origin": row.route["origin"]}
            row.ack_raw = b'{"code":200,"message":"ACK"}' if outcome == "ACK" else None
            row.expires_at = row.created_at + timedelta(seconds=60)
            row.dispatch_started_at = row.created_at
            row.status = "WAITING_RESULT" if outcome == "ACK" else "RECONCILING"
            row.delivery_status = "DONE" if outcome == "ACK" else "HELD"
            row.hold_reason = None if outcome == "ACK" else "DELIVERY_UNKNOWN"
            message = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == view.command_code)
            )
            message.delivery_snapshot = dict(row.delivery_snapshot)
            message.attempts = 1
            message.last_attempt_at = row.dispatch_started_at
            message.last_http_status = 200 if outcome == "ACK" else None
            message.status = row.delivery_status
            message.next_attempt_at = None if outcome == "UNKNOWN" else row.created_at
            message.pause_reason = row.hold_reason
            message.delivery_uncertain = outcome == "UNKNOWN"
            message.expires_at = row.expires_at
            session.add(
                EcsSendEvidence(
                    command_code=view.command_code,
                    lease_id="historic",
                    outcome=outcome,
                    response_raw=row.ack_raw,
                    http_status=200 if outcome == "ACK" else None,
                    created_at=datetime.now(UTC),
                    late=False,
                )
            )
            codes.append(view.command_code)

    settings = main.settings.model_copy(
        update={
            "DATABASE_URL": str(ecs.engine.url.render_as_string(hide_password=False)),
            "RELIABLE_TARGETS": {},
        }
    )
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(main.app.router, "lifespan_context", main.app.router.lifespan_context)
    monkeypatch.setattr(main.app.state, "callback_session_factory", ecs.factory, raising=False)
    original_history = ecs_history_annexes()
    applied = {
        "command_code": codes[0],
        "device_code": "D1",
        "result": "SUCCESS",
        "finish_time": 1,
        "data": {"qty": 1},
    }
    for include_history in (True, False):
        # A distinct application startup, not clearing a conflict within one boot.
        reset_ecs_assembly(main.app, monkeypatch)
        application = create_app(history_annexes=original_history if include_history else None)
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://test",
            ) as client,
        ):
            service = application.state.ecs_commands
            assert ("ecs.dev.protocol", "1") in service.annexes
            assert (("trial", "1") in service.annexes) is include_history
            if include_history:
                assert service.annexes is not original_history
                invalid = await client.post(
                    "/api/v1/callback/result", json={**applied, "data": {"qty": True}}
                )
                assert invalid.status_code == 400
                for code in codes:
                    async with ecs.factory() as session:
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(EcsResultEvidence)
                                .where(EcsResultEvidence.command_code == code)
                            )
                            == 0
                        )
                result = await client.post("/api/v1/callback/result", json=applied)
                assert result.status_code == 200, result.text
                conflict = await client.post(
                    "/api/v1/callback/result", json={**applied, "finish_time": 2}
                )
                assert conflict.status_code == 409
            else:
                async with ecs.factory() as session:
                    original = await session.get(EcsCommand, codes[1])
                    before = (
                        original.status,
                        original.delivery_status,
                        original.applied_result_id,
                        original.hold_reason,
                    )
                unavailable = await client.post(
                    "/api/v1/callback/result", json={**applied, "command_code": codes[1]}
                )
                assert unavailable.status_code == 503
                async with ecs.factory() as session:
                    original = await session.get(EcsCommand, codes[1])
                    assert (
                        original.status,
                        original.delivery_status,
                        original.applied_result_id,
                        original.hold_reason,
                    ) == before
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(EcsResultEvidence)
                            .where(EcsResultEvidence.command_code == codes[1])
                        )
                        == 0
                    )
            duplicate = await client.post("/api/v1/callback/result", json=applied)
            assert duplicate.status_code == 200
            assert duplicate.json() == {"code": 200, "message": "ACK"}
            assert not application.state.ecs_simulator_hits
        assert application.state.reliable_client_provider.closed
    assert list(original_history) == [("trial", "1")]


@pytest.mark.parametrize("assembled", [False, True])
def test_automatic_channel_rejects_parallel_ecs_target_configuration(runtime_settings, assembled):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.integrations.ecs_command.runtime import ecs_runtime_config

    runtime_settings.RELIABLE_TARGETS["targets"]["parallel-ecs"] = {
        "protocol": "ecs-command",
        "base_url": None,
        "address_mode": "SNAPSHOT",
    }
    original = deepcopy(runtime_settings.RELIABLE_TARGETS)
    with pytest.raises(ValueError, match="remove ECS targets from RELIABLE_TARGETS"):
        ecs_runtime_config(
            runtime_settings, SimpleNamespace(target="ecs-local") if assembled else None
        )
    assert runtime_settings.RELIABLE_TARGETS == original
