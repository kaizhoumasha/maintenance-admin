"""Shared input contracts execute only inside the original workline business."""

import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, MetaData, String, Table, delete, select

from app.businesses import catalog
from app.businesses.components import ComponentDefinition
from app.businesses.definitions import DeviceRole, InitialEntry
from app.businesses.runtime import build_runtime, require_component
from app.core.identifiers import uuid7
from app.integrations.ecs_callback import registry as events
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.integrations.registration import RegistrationSource
from app.integrations.wms_callback import Accepted, CallbackPayload
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.operation import Registered
from app.integrations.wms_callback.receiver import receive
from app.integrations.wms_request import operation as requests
from app.models import CallbackReceipt, Device, DeviceRoute, WorkLine
from app.services.device_bindings import resolve_binding
from tests.helpers import add_bound_device, business_composition


@pytest.fixture
async def component_flow(callback_store, monkeypatch):
    metadata = MetaData()
    table = Table(
        "test_component_inputs",
        metadata,
        Column("identity", String, primary_key=True),
        Column("business", String, nullable=False),
        Column("device", String, nullable=False),
        Column("pending", Boolean, nullable=False),
    )
    factory = callback_store
    async with factory() as session:
        engine = session.bind
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)

    class Scan(CallbackPayload):
        bin_code: str

    class Notice(CallbackPayload):
        reference: str

    class Part:
        def __init__(self, owner, capabilities):
            self.owner, self.capabilities = owner, capabilities

        async def accept(self, ctx, payload):
            assert ctx.business_code == self.owner
            if await ctx.session.scalar(
                select(table.c.identity).where(table.c.identity == ctx.digest)
            ):
                return
            await ctx.session.execute(
                table.insert().values(
                    identity=ctx.digest,
                    business=self.owner,
                    device=ctx.device_code,
                    pending=True,
                )
            )

        async def scan_pending(self):
            async with self.capabilities.session_factory() as session, session.begin():
                rows = list(
                    await session.scalars(
                        select(table.c.identity)
                        .where(
                            table.c.business == self.owner,
                            table.c.pending.is_(True),
                        )
                        .limit(5)
                    )
                )
                if rows:
                    await session.execute(
                        table.update().where(table.c.identity.in_(rows)).values(pending=False)
                    )
                return len(rows)

    event_source = RegistrationSource(
        "app.businesses._components.shared_input.ecs_event.scan_completed",
        "scan_completed.py",
        "scan",
        False,
    )
    callback_source = RegistrationSource(
        "app.businesses._components.shared_input.callback.shared_notice_v1",
        "shared_notice_v1.py",
        "notice",
        False,
    )

    async def accept(ctx, payload):
        await require_component(ctx, "shared_input").accept(ctx, payload)

    async def notice(ctx, payload):
        from app.businesses.runtime import bind_callback_business

        owner = {"ref-a": "first", "ref-b": "second", "invalid": "third"}.get(payload.reference)
        if owner is not None:
            bind_callback_business(ctx, owner)
        if payload.reference == "ref-a":
            bind_callback_business(ctx, owner)
        if payload.reference == "conflict":
            bind_callback_business(ctx, "first")
            bind_callback_business(ctx, "second")
        return Accepted()

    component = ComponentDefinition(
        key="shared_input",
        device_roles=(DeviceRole(role_key="SHARED_SCAN", display_name="Shared"),),
        initial_entries=(
            InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role="SHARED_SCAN"),
            InitialEntry(kind="wms_callback", name="shared.notice@v1"),
        ),
        entry_modules=(event_source.module, callback_source.module),
        required_methods=("accept", "scan_pending"),
        requires_progression=True,
    )
    entries = []
    for code in ("first", "second", "third"):
        entry = business_composition(code)
        entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
        entry.ENTRY_MODULES = ()
        entry.PROGRESSION = "scan_pending"
        entry.COMPONENTS = (component,) if code != "third" else ()

        def build(capabilities, code=code):
            part = Part(code, capabilities)

            async def scan_pending():
                return await part.scan_pending()

            return SimpleNamespace(
                components=MappingProxyType({"shared_input": part}), scan_pending=scan_pending
            )

        entry.build_service = build
        entries.append(entry)
    monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(
        events,
        "_registry",
        {("SHARED_SCAN", "SCAN_COMPLETED"): events.Registered(accept, Scan, event_source)},
    )
    monkeypatch.setattr(
        callbacks,
        "_registry",
        {"shared.notice@v1": Registered(notice, Notice, None, (), callback_source)},
    )
    monkeypatch.setattr(requests, "_registry", {})
    catalog.validate_business_contracts()
    runtime = build_runtime(outbox=object(), ecs_commands=None, session_factory=factory)
    line_ids, device_ids = [], []
    async with factory() as session:
        for number, code in enumerate(("first", "second", "first", "third")):
            line, device, _ = await add_bound_device(
                session, f"COMP-{number}", "http://127.0.0.1:1", role="SHARED_SCAN"
            )
            line.business_code = code
            line_ids.append(line.id)
            device_ids.append(device.id)
        await session.commit()
    try:
        yield SimpleNamespace(factory=factory, table=table, runtime=runtime, callbacks=[])
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        async with factory() as session, session.begin():
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.device_code.like("COMP-%"))
            )
            await session.execute(delete(Device).where(Device.id.in_(device_ids)))
            await session.execute(
                delete(DeviceRoute).where(
                    DeviceRoute.device_code.in_([f"COMP-{n}" for n in range(4)])
                )
            )
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(line_ids)))


async def test_one_event_dispatches_only_to_its_workline_business(component_flow):
    flow = component_flow
    for number in range(4):
        async with flow.factory() as session, session.begin():
            response = await receive_ecs_event(
                session,
                json.dumps(
                    {
                        "device_code": f"COMP-{number}",
                        "event_type": "SCAN_COMPLETED",
                        "timestamp": number,
                        "data": {"bin_code": "SAME"},
                    }
                ).encode(),
                resolve_binding=resolve_binding,
                resolve_input_target=catalog.input_target,
                runtime=flow.runtime,
                on_business_committed=flow.callbacks.append,
            )
            assert response.status_code == 200
            assert flow.callbacks == ["first", "second", "first"][:number]
    assert flow.callbacks == ["first", "second", "first"]
    async with flow.factory() as session:
        rows = (await session.execute(select(flow.table))).mappings().all()
    assert {(row["business"], row["device"]) for row in rows} == {
        ("first", "COMP-0"),
        ("second", "COMP-1"),
        ("first", "COMP-2"),
    }
    from app.tasks.business_tasks import advance_pending

    assert await advance_pending(flow.factory, "second") == 1
    async with flow.factory() as session:
        assert list(
            await session.scalars(
                select(flow.table.c.business).where(flow.table.c.pending.is_(True))
            )
        ) == ["first", "first"]


async def test_original_receipt_precedes_dispatch_and_business_dedupe_survives_cleanup(
    component_flow,
):
    flow = component_flow
    raw = json.dumps(
        {
            "device_code": "COMP-0",
            "event_type": "SCAN_COMPLETED",
            "timestamp": 1,
            "data": {"bin_code": "A"},
        }
    ).encode()

    async def deliver(rollback=False):
        async with flow.factory() as session:
            response = await receive_ecs_event(
                session,
                raw,
                resolve_binding=resolve_binding,
                resolve_input_target=catalog.input_target,
                runtime=flow.runtime,
                on_business_committed=flow.callbacks.append,
            )
            if rollback:
                await session.rollback()
            else:
                await session.commit()
            return response

    await deliver(rollback=True)
    assert flow.callbacks == []
    first = await deliver()
    replay = await deliver()
    assert replay.outcome == "duplicate" and replay.body == first.body
    assert flow.callbacks == ["first"]
    async with flow.factory() as session, session.begin():
        await session.execute(
            delete(CallbackReceipt).where(CallbackReceipt.device_code == "COMP-0")
        )
    await deliver()
    async with flow.factory() as session:
        assert len((await session.execute(select(flow.table))).all()) == 1


@pytest.mark.parametrize(
    "reference,code",
    [("ref-a", 202), ("ref-b", 202), ("invalid", 422), ("missing", 422), ("conflict", 422)],
)
async def test_shared_callback_requires_unique_original_business(component_flow, reference, code):
    flow = component_flow
    raw = json.dumps(
        {
            "operation_id": str(uuid7()),
            "operation": "shared.notice@v1",
            "timestamp": 1,
            "data": {"reference": reference},
        }
    ).encode()
    async with flow.factory() as session, session.begin():
        response = await receive(
            session, raw, runtime=flow.runtime, on_business_committed=flow.callbacks.append
        )
        assert response.status_code == code
        assert flow.callbacks == []
    assert flow.callbacks == (
        ["first"] if reference == "ref-a" else ["second"] if reference == "ref-b" else []
    )


async def test_http_input_wakes_business_and_real_task_body_advances_its_component(
    component_flow, monkeypatch
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app import business_wakeups
    from app.controllers.ecs_callback import include_ecs_callback
    from app.core.redis import get_redis
    from app.tasks.business_tasks import advance_pending

    flow = component_flow
    app = FastAPI()
    app.dependency_overrides[get_redis] = lambda: object()
    app.state.callback_session_factory = flow.factory
    app.state.business_committed = business_wakeups.notify_business_committed
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.callbacks.append)
    include_ecs_callback(
        app,
        environment="production",
        resolve_binding=resolve_binding,
        runtime=flow.runtime,
        resolve_input_target=catalog.input_target,
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/callback/event",
            json={
                "device_code": "COMP-1",
                "event_type": "SCAN_COMPLETED",
                "timestamp": 1,
                "data": {"bin_code": "HTTP-BIN"},
            },
        )
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    assert flow.callbacks == ["second"]
    assert await advance_pending(flow.factory, flow.callbacks.pop()) == 1
    async with flow.factory() as session:
        assert (
            await session.execute(select(flow.table.c.business, flow.table.c.pending))
        ).all() == [("second", False)]


@pytest.mark.parametrize("kind", ["ecs", "wms"])
async def test_component_local_entries_keep_local_acceptance_without_formal_business_wakeup(
    component_flow, monkeypatch, kind
):
    flow = component_flow
    handled, sources = [], []

    async def local(ctx, payload):
        handled.append(ctx.receipt_id)
        return Accepted() if kind == "wms" else None

    module = "app.businesses._components.shared_input." + (
        "dev_ecs_event" if kind == "ecs" else "dev_callback"
    )
    source = RegistrationSource(module, module.replace(".", "/") + ".py", "local", True)
    async with flow.factory() as session, session.begin():
        if kind == "ecs":
            monkeypatch.setitem(
                events._registry,
                ("SHARED_SCAN", "DEV_EVENT"),
                events.Registered(local, CallbackPayload, source),
            )
            result = await receive_ecs_event(
                session,
                json.dumps(
                    {"device_code": "COMP-1", "event_type": "DEV_EVENT", "timestamp": 1}
                ).encode(),
                runtime=flow.runtime,
                resolve_binding=resolve_binding,
                resolve_input_target=catalog.input_target,
                on_committed=sources.append,
                on_business_committed=flow.callbacks.append,
            )
            assert result.status_code == 200
        else:
            monkeypatch.setitem(
                callbacks._registry,
                "shared.dev_notice@v1",
                Registered(local, CallbackPayload, None, (), source),
            )
            result = await receive(
                session,
                json.dumps(
                    {
                        "operation_id": str(uuid7()),
                        "operation": "shared.dev_notice@v1",
                        "timestamp": 1,
                        "data": {},
                    }
                ).encode(),
                runtime=flow.runtime,
                on_committed=sources.append,
                on_business_committed=flow.callbacks.append,
            )
            assert result.status_code == 202
        assert result.outcome == "received"
        assert sources == []
    assert handled and sources == [source]
    assert flow.callbacks == []
