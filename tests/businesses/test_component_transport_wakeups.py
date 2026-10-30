import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, String, Table, delete, event, func, select

from app import business_wakeups
from app.businesses import catalog
from app.businesses.components import ComponentDefinition
from app.businesses.runtime import build_runtime
from app.controllers import wms_events
from app.controllers.wms_events import include_wms_events
from app.core.identifiers import uuid7
from app.core.redis import get_redis
from app.integrations.ecs_callback import registry as events
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.receiver import receive
from app.integrations.wms_request import operation as requests
from app.integrations.wms_transport import (
    ADAPTERS,
    PROTOCOL,
    BinMove,
    HandoffPosition,
    Rack,
    RackBinSlot,
    RackPosition,
    TransportCaller,
    TransportService,
)
from app.models import ReliableMessage, TransportTask
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.tasks.business_tasks import advance_pending
from tests.helpers import business_composition

RESULT = "transport.task.resulted@v1"
POSITION = "transport.task.member_position_changed@v1"


def slot(number):
    return {"kind": "RACK_BIN_SLOT", "rack_id": "R1", "rack_face": "90", "slot_id": number}


def result_data(handle, *, bins=False, status="SUCCEEDED", revision=1):
    data = {
        "transport_task_id": handle.transport_task_id,
        "kind": "BIN_MOVE" if bins else "RACK_MOVE",
        "outcome_revision": revision,
    }
    if bins:
        data["results"] = [
            {"container_id": "B1", "status": "SUCCEEDED", "final_position": slot("S1")},
            {"container_id": "B2", "status": "SUCCEEDED", "final_position": slot("S2")},
        ]
    else:
        data.update(
            rack_id="R1",
            status=status,
            final_position={"kind": "RACK_POSITION", "location_code": "TARGET"},
        )
        if status == "UNKNOWN":
            data.update(
                status="FAILED",
                failure_code="POSITION_UNKNOWN",
                position_unknown=True,
                final_position=None,
            )
        elif status == "FAILED":
            data["failure_code"] = "RCS_EXECUTION_FAILED"
    return data


@pytest.fixture
async def transport_flow(callback_store, monkeypatch):
    factory = callback_store
    engine = factory.kw["bind"]
    saved_examples = dict(wms_events.EXAMPLES)
    metadata = MetaData()
    table = Table(
        "test_shared_transport_pending",
        metadata,
        Column("client_id", String, primary_key=True),
        Column("task_id", String, nullable=False),
        Column("business", String, nullable=False),
        Column("revision", Integer, nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    try:
        component = ComponentDefinition(
            key="shared_transport", required_methods=("scan_pending",), requires_progression=True
        )
        entries = [business_composition(code) for code in ("first", "second")]

        class Part:
            def __init__(self, business, capabilities):
                self.business, self.capabilities = business, capabilities

            async def scan_pending(self):
                async with self.capabilities.session_factory() as session:
                    rows = (
                        (
                            await session.execute(
                                select(table)
                                .where(table.c.business == self.business)
                                .order_by(table.c.client_id)
                                .limit(5)
                            )
                        )
                        .mappings()
                        .all()
                    )
                advanced = 0
                for row in rows:
                    async with self.capabilities.session_factory() as session, session.begin():
                        if session.get_bind().dialect.name == "postgresql":
                            await session.connection(
                                execution_options={"isolation_level": "REPEATABLE READ"}
                            )
                        current = (
                            (
                                await session.execute(
                                    select(table)
                                    .where(table.c.client_id == row["client_id"])
                                    .with_for_update()
                                )
                            )
                            .mappings()
                            .one()
                        )
                        outcome = await self.capabilities.transport.get_result(
                            session,
                            transport_task_id=row["task_id"],
                            client_request_id=row["client_id"],
                            caller=TransportCaller("line-" + self.business),
                        )
                        if (
                            outcome.outcome_revision is not None
                            and outcome.outcome_revision > current["revision"]
                        ):
                            await session.execute(
                                table.update()
                                .where(table.c.client_id == row["client_id"])
                                .values(revision=outcome.outcome_revision)
                            )
                            advanced += 1
                return advanced

        for entry in entries:
            entry.DEFINITION = replace(entry.DEFINITION, initial_entries=())
            entry.ENTRY_MODULES = ()
            entry.COMPONENTS = (component,)
            entry.PROGRESSION = "scan_pending"

            def build(capabilities, code=entry.DEFINITION.code):
                part = Part(code, capabilities)
                return SimpleNamespace(
                    capabilities=capabilities,
                    components=MappingProxyType({"shared_transport": part}),
                    scan_pending=part.scan_pending,
                )

            entry.build_service = build
        monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
        monkeypatch.setattr(catalog, "_validated_entries", None)
        monkeypatch.setattr(events, "_registry", {})
        monkeypatch.setattr(requests, "_registry", {})
        platform = {
            key: item
            for key, item in callbacks.operations().items()
            if item.registration_source.module.startswith("app.integrations.wms_transport.")
        }
        assert set(platform) == {RESULT, POSITION}
        monkeypatch.setattr(callbacks, "_registry", platform)
        catalog.validate_business_contracts()
        target = TargetConfig("component-transport", "http://example.test", PROTOCOL)
        outbox = Outbox({target.name: target}, ADAPTERS)
        runtime = build_runtime(outbox=outbox, ecs_commands=None, session_factory=factory)
        state = SimpleNamespace(notices=[], fail_publish=False)

        def publish(business):
            if state.fail_publish:
                raise RuntimeError("synthetic broker unavailable")
            state.notices.append(business)

        state.real_publisher = business_wakeups.enqueue_business
        monkeypatch.setattr(business_wakeups, "enqueue_business", publish)
        app = FastAPI()
        app.dependency_overrides[get_redis] = lambda: object()
        app.state.callback_session_factory = factory
        app.state.business_committed = business_wakeups.notify_business_committed
        app.state.business_event_committed = business_wakeups.event_committed
        include_wms_events(app, environment="production", runtime=runtime)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:

            async def register(business="first", *, bins=False, legacy=False):
                service = runtime.services[business].capabilities.transport
                if legacy:
                    service = TransportService(outbox)
                common = dict(
                    client_request_id=str(uuid7()), caller=TransportCaller("line-" + business)
                )
                async with factory.begin() as session:
                    if bins:
                        handle = await service.move_bins(
                            session,
                            **common,
                            moves=[
                                BinMove(
                                    "B1", HandoffPosition("CNV"), RackBinSlot("R1", "90", "S1")
                                ),
                                BinMove(
                                    "B2", HandoffPosition("CNV"), RackBinSlot("R1", "90", "S2")
                                ),
                            ],
                        )
                    else:
                        handle = await service.move_rack(
                            session,
                            **common,
                            rack_id="R1",
                            source=Rack("R1"),
                            target=RackPosition("TARGET"),
                            target_face="90",
                        )
                    await session.execute(
                        table.insert().values(
                            client_id=handle.client_request_id,
                            task_id=handle.transport_task_id,
                            business=business,
                            revision=0,
                        )
                    )
                return handle

            async def post(operation, data, *, identity=None):
                return await client.post(
                    "/api/v1/wms/events",
                    json={
                        "operation_id": identity or str(uuid7()),
                        "operation": operation,
                        "timestamp": 1,
                        "data": data,
                    },
                )

            state.factory, state.table, state.app = factory, table, app
            state.register, state.post, state.outbox, state.targets = (
                register,
                post,
                outbox,
                outbox.targets,
            )
            yield state
    finally:
        wms_events.EXAMPLES.clear()
        wms_events.EXAMPLES.update(saved_examples)
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
        async with factory.begin() as session:
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == "component-transport")
            )
            await session.execute(
                delete(TransportTask).where(
                    TransportTask.workline_id.in_(["line-first", "line-second"])
                )
            )


@pytest.mark.parametrize("order", ["result_first", "position_first"])
async def test_bin_result_wakes_only_after_actual_application(transport_flow, order):
    flow = transport_flow
    first = await flow.register(bins=True)
    await flow.register("second")
    data = result_data(first, bins=True)
    if order == "result_first":
        assert (await flow.post(RESULT, data)).status_code == 202
        assert flow.notices == []
    for member, number in (("B1", "S1"), ("B2", "S2")):
        placed = {
            "transport_task_id": first.transport_task_id,
            "container_id": member,
            "milestone": "TARGET_PLACED",
            "final_position": slot(number),
        }
        assert (await flow.post(POSITION, placed)).status_code == 202
        if member == "B1" or order == "position_first":
            assert flow.notices == []
    if order == "position_first":
        assert (await flow.post(RESULT, data)).status_code == 202
    assert flow.notices == ["first"]
    assert (await flow.post(RESULT, data)).status_code == 200
    assert flow.notices == ["first"]
    assert await advance_pending(flow.factory, flow.notices.pop()) == 1
    assert await advance_pending(flow.factory, "first") == 0
    async with flow.factory() as session:
        assert (
            await session.execute(
                select(flow.table.c.business, flow.table.c.revision).order_by(flow.table.c.business)
            )
        ).all() == [("first", 1), ("second", 0)]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target == "component-transport")
            )
            == 2
        )


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"])
async def test_applied_outcome_versions_wake_original_business(transport_flow, status):
    flow = transport_flow
    handle = await flow.register()
    assert (await flow.post(RESULT, result_data(handle, status=status))).status_code == 202
    assert flow.notices == ["first"]
    assert await advance_pending(flow.factory, "first") == 1
    if status == "UNKNOWN":
        assert (await flow.post(RESULT, result_data(handle, revision=2))).status_code == 202
        assert flow.notices == ["first", "first"]
        assert await advance_pending(flow.factory, "first") == 1
    else:
        assert (
            await flow.post(RESULT, result_data(handle, status="FAILED", revision=2))
        ).status_code == 202
        assert flow.notices == ["first"]


async def test_publish_failure_keeps_applied_fact_and_beat_recovers_original_intent(transport_flow):
    flow = transport_flow
    handle = await flow.register()
    flow.fail_publish = True
    assert (await flow.post(RESULT, result_data(handle))).status_code == 202
    assert flow.notices == []
    flow.fail_publish = False
    assert await advance_pending(flow.factory, "first") == 1
    async with flow.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert task.owner_domain == "first" and task.applied_outcome_revision == 1
        assert task.client_request_id == handle.client_request_id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ReliableMessage)
                .where(ReliableMessage.target == "component-transport")
            )
            == 1
        )


@pytest.mark.parametrize("end", ["rollback", "close", "savepoint"])
async def test_uncommitted_transport_application_never_wakes(transport_flow, end):
    flow = transport_flow
    handle = await flow.register()
    raw = json.dumps(
        {
            "operation_id": str(uuid7()),
            "operation": RESULT,
            "timestamp": 1,
            "data": result_data(handle),
        }
    ).encode()
    async with flow.factory() as session:
        nested = await session.begin_nested() if end == "savepoint" else None
        reply = await receive(
            session, raw, on_owner_committed=business_wakeups.notify_business_committed
        )
        assert reply.status_code == 202 and flow.notices == []
        if nested is not None:
            await nested.rollback()
            await session.commit()
        elif end == "close":
            await session.close()
        else:
            await session.rollback()
    assert flow.notices == []
    async with flow.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert task.applied_outcome_revision is None


async def test_commit_failure_rolls_back_application_and_notification(transport_flow):
    flow = transport_flow
    handle = await flow.register()

    def factory():
        session = flow.factory()

        def fail_outer(sync_session):
            if not sync_session.in_nested_transaction():
                raise ValueError("synthetic outer commit failure")

        event.listen(session.sync_session, "before_commit", fail_outer)
        return session

    flow.app.state.callback_session_factory = factory
    response = await flow.post(RESULT, result_data(handle))
    assert response.status_code == 503 and flow.notices == []
    async with flow.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert task.applied_outcome_revision is None


async def test_legacy_unowned_task_is_not_guessed_and_known_pending_can_recover(transport_flow):
    flow = transport_flow
    handle = await flow.register(legacy=True)
    assert (await flow.post(RESULT, result_data(handle))).status_code == 202
    assert flow.notices == []
    assert await advance_pending(flow.factory, "first") == 1
    async with flow.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert task.owner_domain is None


@pytest.mark.parametrize("case", ["unknown_task", "missing_frozen", "wrong_rack", "position_only"])
async def test_unapplied_transport_facts_never_notify_an_owner(transport_flow, case):
    flow = transport_flow
    handle = await flow.register()
    data = result_data(handle)
    operation = RESULT
    if case == "unknown_task":
        data["transport_task_id"] = "transport-" + str(uuid7())
    elif case == "missing_frozen":
        async with flow.factory.begin() as session:
            row = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            row.frozen = None
    elif case == "wrong_rack":
        data["rack_id"] = "other-rack"
    else:
        operation = POSITION
        data = {
            "transport_task_id": handle.transport_task_id,
            "container_id": "B1",
            "milestone": "SOURCE_PICKED",
        }
    assert (await flow.post(operation, data)).status_code == 202
    assert flow.notices == []
    async with flow.factory() as session:
        row = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
        assert row.applied_outcome_revision is None


async def test_submit_ack_does_not_wake_business_or_prove_application(transport_flow):
    from app.http_client import DeliveryState, HttpResult
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus

    flow = transport_flow
    handle = await flow.register()
    store = MessageStore(flow.factory, on_result=business_wakeups.wms_result_committed)
    [claim] = await store.claim(1, flow.targets)
    raw = json.dumps(
        {
            "operation_id": claim.message.operation_id,
            "code": "RECEIVED",
            "timestamp": 1,
            "data": {"transport_task_id": handle.transport_task_id},
        }
    ).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw)
    verdict = ADAPTERS[PROTOCOL].classify_response(claim.message, response)
    assert await store.finish(
        claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    )
    assert flow.notices == []
    assert await advance_pending(flow.factory, "first") == 0


async def test_transport_notification_crosses_real_memory_broker_and_runs_original_task(
    transport_flow, monkeypatch
):
    from celery import Celery

    from app.tasks import business_tasks

    flow = transport_flow
    app = Celery(
        "transport-result-publication",
        broker="memory://",
        backend="redis://127.0.0.1:1/0",
        set_as_current=False,
    )
    app.conf.task_default_queue = "transport_owner_" + uuid7().hex

    def pending(business):
        pass

    task = app.task(name="business.advance_pending", shared=False, lazy=False)(pending)
    monkeypatch.setattr(business_tasks, "advance_business_pending", task)
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.real_publisher)

    def no_result_backend(*args, **kwargs):
        raise AssertionError("publication must ignore the result backend")

    monkeypatch.setattr(app.backend, "on_task_call", no_result_backend)
    try:
        handle = await flow.register()
        assert (await flow.post(RESULT, result_data(handle))).status_code == 202
        with app.connection_for_read() as connection:
            queue = connection.SimpleQueue(app.conf.task_default_queue)
            try:
                message = queue.get(block=False)
                assert message.headers["task"] == "business.advance_pending"
                assert message.payload[0] == ["first"]
                assert await advance_pending(flow.factory, message.payload[0][0]) == 1
                message.ack()
            finally:
                queue.close()
    finally:
        app.close()
