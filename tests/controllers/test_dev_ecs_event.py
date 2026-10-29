"""Local event debugging exercises the public receiver without changing event identity."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ConfigDict
from sqlalchemy import func, select

from app.controllers.dev import include_dev_routers
from app.controllers.ecs_callback import include_ecs_callback
from app.core import register_exception_handlers
from app.integrations.ecs_callback import CallbackPayload, ecs_event, registry
from app.integrations.ecs_callback.envelope import EcsEventEnvelope
from app.integrations.ecs_callback.receiver import MAX_BODY_BYTES, event_identity
from app.main import app as main_app
from app.models import CallbackReceipt
from tests.helpers import ecs_binding

URL = "/api/v1/dev/ecs/events"


@pytest.fixture
async def event_debug(client, admin, callback_db, monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})

    class Scan(CallbackPayload):
        barcode: str
        model_config = ConfigDict(json_schema_extra={"examples": [{"barcode": "TEST-1"}]})

    async def accept(ctx, payload):
        ctx.note("TEST_ACCEPTED", barcode=payload.barcode)

    ecs_event("SCAN_COMPLETED", device_role="SCAN1", data=Scan)(accept)
    application = FastAPI()
    register_exception_handlers(application)
    application.state.callback_session_factory = callback_db
    application.dependency_overrides.update(main_app.dependency_overrides)
    include_ecs_callback(application, environment="local", resolve_binding=ecs_binding)
    include_dev_routers(application, environment="local", command_bindings=())
    body = {
        "device_code": "TEST_DEVICE",
        "event_type": "SCAN_COMPLETED",
        "timestamp": 1793206799000,
        "data": {"barcode": "TEST-1", "extra_fact": 7},
    }
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://test"
    ) as http:
        yield SimpleNamespace(
            client=http, headers=admin, app=application, factory=callback_db, body=body, model=Scan
        )


async def test_event_debug_requires_authentication(event_debug):
    assert (await event_debug.client.post(URL, json=event_debug.body)).status_code == 401
    assert (await event_debug.client.get(URL + "/" + "a" * 64)).status_code == 401


async def test_debug_preserves_original_data_identity_receipt_and_diagnostics(event_debug):
    e = event_debug
    expected = event_identity(EcsEventEnvelope.model_validate(e.body))
    response = await e.client.post(URL, json=e.body, headers=e.headers)
    assert response.status_code == 200, response.text
    delivered = response.json()
    assert delivered["digest"] == expected
    assert delivered["outcome"] == "received"
    assert delivered["response"] == {"code": 200, "message": "ACK"}
    assert delivered["diagnostic"] == [{"reason_code": "TEST_ACCEPTED", "barcode": "TEST-1"}]
    duplicate = await e.client.post(URL, json=e.body, headers=e.headers)
    assert duplicate.json()["outcome"] == "duplicate"
    assert duplicate.json()["diagnostic"] == delivered["diagnostic"]
    public = await e.client.post("/api/v1/callback/event", json=e.body)
    assert public.json() == delivered["response"]
    state = await e.client.get(URL + "/" + expected, headers=e.headers)
    assert state.status_code == 200, state.text
    assert state.json()["receipt"]["response"] == delivered["response"]
    assert state.json()["receipt"]["diagnostic"] == delivered["diagnostic"]
    assert state.json()["business"] is None
    async with e.factory() as session:
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 1


@pytest.mark.parametrize("data", [{}, {"barcode": 12}])
async def test_debug_reports_payload_rejection_without_creating_a_receipt(event_debug, data):
    e = event_debug
    response = await e.client.post(URL, json={**e.body, "data": data}, headers=e.headers)
    assert response.status_code == 200, response.text
    assert response.json()["status_code"] == 400
    assert response.json()["response"]["message"] == "INVALID_ENVELOPE"
    assert response.json()["digest"] is None
    async with e.factory() as session:
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 0


async def test_debug_keeps_unregistered_diagnostic_instead_of_claiming_acceptance(event_debug):
    e = event_debug
    response = await e.client.post(
        URL, json={**e.body, "event_type": "UNREGISTERED"}, headers=e.headers
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "unregistered"
    assert response.json()["diagnostic"][0]["reason_code"] == "UNREGISTERED_EVENT_TYPE"


async def test_debug_bounds_body_and_rolls_back_failed_handler(event_debug, monkeypatch):
    e = event_debug
    large = await e.client.post(URL, content=b"x" * (MAX_BODY_BYTES + 1), headers=e.headers)
    assert large.status_code == 200
    assert large.json()["status_code"] == 413

    async def fail(ctx, payload):
        raise RuntimeError("simulated storage failure")

    from dataclasses import replace

    item = registry.lookup("SCAN1", "SCAN_COMPLETED")
    monkeypatch.setitem(registry._registry, ("SCAN1", "SCAN_COMPLETED"), replace(item, func=fail))
    failed = await e.client.post(URL, json=e.body, headers=e.headers)
    assert failed.status_code == 200
    assert failed.json()["status_code"] == 503
    assert failed.json()["outcome"] == "error"
    async with e.factory() as session:
        assert await session.scalar(select(func.count()).select_from(CallbackReceipt)) == 0


@pytest.mark.parametrize("digest,status", [("bad", 400), ("b" * 64, 404)])
async def test_event_query_rejects_invalid_or_unknown_identity(event_debug, digest, status):
    response = await event_debug.client.get(URL + "/" + digest, headers=event_debug.headers)
    assert response.status_code == status, response.text


async def test_swagger_event_examples_use_roles_and_registered_payloads(event_debug):
    schema = event_debug.app.openapi()
    operation = schema["paths"].get(URL, {}).get("post")
    assert operation is not None, "local event debugging is missing"
    examples = operation["requestBody"]["content"]["application/json"]["examples"]
    example = next(item for item in examples.values() if "SCAN1" in item["summary"])
    assert example["value"]["event_type"] == "SCAN_COMPLETED"
    assert example["value"]["data"] == {"barcode": "TEST-1"}


@pytest.mark.parametrize("roles", [(), ("ROLE_A", "ROLE_B")])
async def test_swagger_handles_empty_registry_and_same_name_roles(event_debug, monkeypatch, roles):
    from app.controllers.dev_ecs_event import ecs_event_router

    monkeypatch.setattr(registry, "_registry", {})
    for role in roles:

        async def accept(ctx, payload):
            pass

        ecs_event("SHARED_EVENT", device_role=role, data=event_debug.model)(accept)
    application = FastAPI()
    application.include_router(ecs_event_router(), prefix="/api/v1")
    operation = application.openapi()["paths"][URL]["post"]
    examples = operation["requestBody"]["content"]["application/json"].get("examples", {})
    assert len(examples) == len(roles)
    assert {item["summary"] for item in examples.values()} == {
        f"{role} / SHARED_EVENT" for role in roles
    }


async def test_event_query_projects_owned_state_in_one_read_only_snapshot(event_debug):
    from sqlalchemy import Boolean, Column, MetaData, String, Table, delete, text, update

    from app.controllers.dev_ecs_event import ecs_event_router
    from app.core.identifiers import uuid7
    from app.database import SCHEMA

    e = event_debug
    engine = e.factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL repeatable-read snapshot semantics")
    owned = Table(
        "event_projection_" + uuid7().hex,
        MetaData(schema=SCHEMA),
        Column("digest", String(64), primary_key=True),
        Column("pending", Boolean),
    )
    async with engine.begin() as connection:
        await connection.run_sync(owned.create)
    try:
        response = await e.client.post(URL, json=e.body, headers=e.headers)
        digest = response.json()["digest"]
        async with e.factory.begin() as session:
            await session.execute(owned.insert().values(digest=digest, pending=True))
        changed = False

        async def project(session, key):
            nonlocal changed
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"
            if not changed:
                async with e.factory.begin() as writer:
                    await writer.execute(delete(CallbackReceipt))
                    await writer.execute(update(owned).values(pending=False))
                changed = True
            row = (
                (await session.execute(select(owned).where(owned.c.digest == key))).mappings().one()
            )
            return dict(row)

        application = FastAPI()
        register_exception_handlers(application)
        application.state.callback_session_factory = e.factory
        application.dependency_overrides.update(e.app.dependency_overrides)
        application.include_router(ecs_event_router(read_business=project), prefix="/api/v1")
        async with AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://test",
        ) as reader:
            first = await reader.get(URL + "/" + digest, headers=e.headers)
            assert first.status_code == 200, first.text
            assert first.json()["receipt"] is not None
            assert first.json()["business"]["pending"] is True
            second = await reader.get(URL + "/" + digest, headers=e.headers)
            assert second.status_code == 200
            assert second.json()["receipt"] is None
            assert second.json()["business"]["pending"] is False
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(owned.drop)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_nonlocal_application_has_no_event_debug_routes(environment):
    application = FastAPI()
    include_dev_routers(application, environment=environment, command_bindings=())
    assert URL not in application.openapi()["paths"]


def test_swagger_event_without_payload_example_points_to_registry(monkeypatch):
    from app.controllers.dev_ecs_event import ecs_event_router

    monkeypatch.setattr(registry, "_registry", {})

    class Scan(CallbackPayload):
        barcode: str

    @ecs_event("SCAN_COMPLETED", device_role="INPUT_ARM", data=Scan)
    async def accept(ctx, payload):
        pass

    application = FastAPI()
    application.include_router(ecs_event_router(), prefix="/api/v1")
    media = application.openapi()["paths"][URL]["post"]["requestBody"]["content"][
        "application/json"
    ]
    example = media["examples"]["INPUT_ARM:SCAN_COMPLETED"]
    assert example["value"]["data"] == {}
    assert "data" in example["description"] and "注册目录" in example["description"]


@pytest.mark.parametrize("failure", ["storage", "projection"])
async def test_query_unavailability_is_safe_and_does_not_erase_persisted_receipt(
    event_debug, failure
):
    from sqlalchemy.exc import OperationalError

    from app.controllers.dev_ecs_event import ecs_event_router

    e = event_debug
    delivered = await e.client.post(URL, json=e.body, headers=e.headers)
    digest = delivered.json()["digest"]
    secret = "PRIVATE_QUERY_FAILURE"

    def unavailable():
        raise OperationalError("SELECT", {}, RuntimeError(secret))

    async def project(session, key):
        unavailable()

    application = FastAPI()
    register_exception_handlers(application)
    application.state.callback_session_factory = unavailable if failure == "storage" else e.factory
    application.dependency_overrides.update(e.app.dependency_overrides)
    application.include_router(ecs_event_router(read_business=project), prefix="/api/v1")
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://test"
    ) as reader:
        response = await reader.get(URL + "/" + digest, headers=e.headers)
    assert response.status_code == 503, response.text
    assert secret not in response.text
    original = await e.client.get(URL + "/" + digest, headers=e.headers)
    assert original.status_code == 200
    assert original.json()["receipt"]["response"] == delivered.json()["response"]
