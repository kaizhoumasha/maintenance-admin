"""Test-only AUTO composition and independent receiver tables; no formal activation."""

import importlib
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.businesses import catalog
from app.businesses._components.rack_dispatch.models import RackAction, RackJob
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses._components.sorting_task.composition import COMPONENT
from app.businesses._components.sorting_task.service import SortingTask
from app.businesses.definitions import BusinessDefinition
from app.businesses.runtime import Capabilities, build_runtime
from app.businesses.smt_auto.models import TABLES, AutoBinWork, AutoTask, AutoTaskRequest
from app.businesses.smt_auto.transactions import AutoS1Transactions
from app.core.clock import unix_ms
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.ecs_callback import registry as events
from app.integrations.wms_callback import registry as callbacks
from app.integrations.wms_callback.receiver import receive
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL, WmsRequests
from app.integrations.wms_request import operation as registry
from app.integrations.wms_request import operation as requests
from app.integrations.wms_transport import ADAPTERS as TRANSPORT_ADAPTERS
from app.integrations.wms_transport import TransportService
from app.models import (
    Device,
    DeviceRoute,
    IntegrationExchange,
    ReliableMessage,
    TransportTask,
    WorkLine,
    WorkLinePosition,
)
from app.models.base import Base
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from tests.helpers import business_composition, copy_history_tables


@pytest.fixture(params=["sqlite", "postgres"])
async def auto_receiver(request, monkeypatch, postgres_tables):
    metadata = MetaData(schema=Base.metadata.schema)
    for table in [
        *TABLES.values(),
        *(
            m.__table__
            for m in (
                WorkLine,
                DeviceRoute,
                Device,
                RackJob,
                RackAction,
                IntegrationExchange,
                ReliableMessage,
            )
        ),
    ]:
        table.to_metadata(metadata)
    async with AsyncExitStack() as stack:
        if request.param == "postgres":
            url = os.environ.get("TEST_DATABASE_URL")
            if not url:
                pytest.skip(
                    "TEST_DATABASE_URL required for isolated AUTO receiver PostgreSQL tests"
                )
            engine = await stack.enter_async_context(
                postgres_tables(url, "auto_receiver", metadata)
            )
        else:
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                execution_options={"schema_translate_map": {Base.metadata.schema: None}},
            )
            stack.push_async_callback(engine.dispose)
            async with engine.begin() as connection:
                copy_history_tables(metadata)
                await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        entry = business_composition("smt_auto")
        entry.DEFINITION = BusinessDefinition(code="smt_auto", display_name="Isolated AUTO S1 test")
        entry.ENTRY_MODULES, entry.COMPONENTS, entry.PROGRESSION = (), (COMPONENT,), "scan_pending"

        def build(capabilities):
            transactions = AutoS1Transactions(capabilities)

            async def scan_pending():
                raise AssertionError("S1 test assembly never schedules production")

            return SimpleNamespace(
                components={"sorting_task": SortingTask("smt_auto", transactions.host)},
                transactions=transactions,
                scan_pending=scan_pending,
            )

        entry.build_service = build
        monkeypatch.setattr(catalog, "entries", lambda: (entry,))
        monkeypatch.setattr(catalog, "_validated_entries", None)
        monkeypatch.setattr(events, "_registry", {})
        monkeypatch.setattr(callbacks, "_registry", {})
        monkeypatch.setattr(requests, "_registry", {})
        original_modules = {
            name: sys.modules[name] for name in COMPONENT.entry_modules if name in sys.modules
        }
        try:
            for name in COMPONENT.entry_modules:
                if ".request." in name and name in sys.modules:
                    model = "Prepare" if name.endswith("prepare_v1") else "CompletionConfirm"
                    operation = (
                        "sorting.task.prepare@v1"
                        if model == "Prepare"
                        else "sorting.task.completion_confirm@v1"
                    )
                    requests.request_operation(operation, requests.Endpoint.DECISIONS)(
                        getattr(sys.modules[name], model)
                    )
                elif name in sys.modules:
                    importlib.reload(sys.modules[name])
                else:
                    importlib.import_module(name)
            catalog.validate_business_contracts()
            target = TargetConfig("auto-test-decisions", "http://example.test", DECISIONS_PROTOCOL)
            targets = {target.name: target}
            runtime = build_runtime(
                outbox=Outbox(targets, ADAPTERS, records=history_records),
                ecs_commands=None,
                session_factory=factory,
            )
            async with factory.begin() as session:
                line = WorkLine(
                    line_code="AUTO-TEST",
                    line_name="AUTO",
                    business_code="smt_auto",
                    is_enabled=True,
                )
                session.add(line)
                await session.flush()
                line_id = line.id
            notices = []

            def message(name, data):
                return dict(
                    operation=f"sorting.task.{name}@v1",
                    operation_id=str(uuid7()),
                    timestamp=unix_ms(datetime.now(UTC)),
                    data=data,
                )

            async def deliver(body, *, rollback=False):
                async with factory() as session:
                    try:
                        if session.get_bind().dialect.name == "postgresql":
                            await session.connection(
                                execution_options={"isolation_level": "REPEATABLE READ"}
                            )
                            assert (
                                await session.scalar(text("SHOW transaction_isolation"))
                                == "repeatable read"
                            )
                        response = await receive(
                            session,
                            json.dumps(body).encode(),
                            runtime=runtime,
                            on_business_committed=notices.append,
                        )
                        if rollback:
                            await session.rollback()
                        else:
                            await session.commit()
                        return response
                    except BaseException:
                        await session.rollback()
                        raise

            async def accept_prepare():
                store = MessageStore(factory, records=history_records)
                claims = await store.claim(1, targets)
                assert len(claims) == 1
                claim = claims[0]
                raw = json.dumps(
                    dict(
                        operation_id=claim.message.operation_id,
                        code="PREPARE_ACCEPTED",
                        timestamp=1,
                        data={},
                    )
                ).encode()
                response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw)
                verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
                assert await store.finish(
                    claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
                )

            yield SimpleNamespace(
                factory=factory,
                runtime=runtime,
                transactions=runtime.services["smt_auto"].transactions,
                line_id=line_id,
                notices=notices,
                message=message,
                deliver=deliver,
                accept_prepare=accept_prepare,
            )
        finally:
            for name in COMPONENT.entry_modules:
                if name in original_modules:
                    sys.modules[name] = original_modules[name]
                else:
                    sys.modules.pop(name, None)


@pytest.fixture
async def direction_host(monkeypatch):
    entry = business_composition("smt_auto")
    entry.DEFINITION = BusinessDefinition(code="smt_auto", display_name="Isolated AUTO")
    entry.ENTRY_MODULES = ("app.businesses.smt_auto.request",)
    entry.COMPONENTS = (COMPONENT,)
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(registry, "_registry", {})
    for name, model in (("prepare", "Prepare"), ("completion_confirm", "CompletionConfirm")):
        module = f"app.businesses._components.sorting_task.request.sorting_task_{name}_v1"
        if module in sys.modules:
            registry.request_operation(f"sorting.task.{name}@v1", registry.Endpoint.DECISIONS)(
                getattr(sys.modules[module], model)
            )
        else:
            importlib.import_module(module)
    for suffix in (
        "putaway_work_plan_v1",
        "outbound_bin_work_plan_v1",
        "outbound_return_rack_work_plan_v1",
    ):
        name = f"app.businesses.smt_auto.request.{suffix}"
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    metadata = MetaData(schema=Base.metadata.schema)
    for table in (
        *TABLES.values(),
        *(
            model.__table__
            for model in (
                WorkLine,
                ReliableMessage,
                WorkLinePosition,
                Device,
                DeviceRoute,
                TransportTask,
                RackJob,
                RackAction,
            )
        ),
    ):
        table.to_metadata(metadata)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        execution_options={"schema_translate_map": {Base.metadata.schema: None}},
    )
    async with engine.begin() as connection:
        copy_history_tables(metadata)
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    target = TargetConfig("decisions", "http://example.test", DECISIONS_PROTOCOL)
    requests = WmsRequests(
        Outbox({target.name: target}, ADAPTERS, records=history_records),
        owner_domain="smt_auto",
        contract_consumers=lambda _: frozenset({"smt_auto"}),
    )
    async with factory.begin() as session:
        session.add(
            WorkLine(
                id=1, line_code="LINE", line_name="line", business_code="smt_auto", is_enabled=True
            )
        )
        session.add(
            AutoTask(
                task_id="t",
                business_type="outbound",
                work_line_id=1,
                workline_code="LINE",
                issued_operation_id="issued",
                issued_timestamp=1,
                issued_data={},
                status="EXECUTING",
                dispatch_sequence=1,
                plan_revision=0,
                prepare_accepted_at_ms=1,
            )
        )
        await session.flush()
        session.add(
            AutoTaskRequest(
                task_id="t",
                purpose="PREPARE",
                operation="sorting.task.prepare@v1",
                operation_id="prepare",
                message_id=100,
                request_data={"task_id": "t", "workline_code": "LINE"},
                response_snapshot={"http_status": 202, "code": "PREPARE_ACCEPTED", "data": {}},
                consumed_at_ms=1,
                unresolved=False,
            )
        )
        session.add(
            AutoBinWork(
                work_line_id=1,
                arrival_id="arrival",
                device_code="SCAN2",
                raw_bin_code="BIN-C",
                bin_code="BIN",
                scanned_at=10,
                task_id="t",
                business_type="outbound",
                association="TASK",
                pending=True,
            )
        )
    transport_target = TargetConfig("transport", "http://example.test", "wms-transport")
    transport = TransportService(
        Outbox(
            {transport_target.name: transport_target}, TRANSPORT_ADAPTERS, records=history_records
        ),
        owner_domain="smt_auto",
    )

    async def allows_task_entry(session, *, work_line_id, task_id):
        return work_line_id == 1 and task_id == "t"

    dispatch = RackDispatch(
        "smt_auto",
        Capabilities(None, requests, transport, factory),
        task_admission=SimpleNamespace(allows_task_entry=allows_task_entry),
    )
    yield SimpleNamespace(factory=factory, requests=requests, dispatch=dispatch, entry=entry)
    await engine.dispose()


@pytest.fixture
async def s5_roller(direction_host, monkeypatch):
    """An actual isolated roller, event receiver, ECS outbox and result lifecycle."""
    from dataclasses import replace

    from app.businesses._components.bin_roller.composition import COMPONENT as ROLLER
    from app.businesses._components.bin_roller.ecs_command import MoveForward, MoveLeft, MoveRight
    from app.businesses._components.bin_roller.models import TABLES as ROLLER_TABLES
    from app.businesses._components.bin_roller.service import BinRoller
    from app.businesses.catalog import InputTarget
    from app.businesses.runtime import BusinessRuntime
    from app.businesses.smt_auto.roller_ports import RollerPorts
    from app.businesses.smt_auto.work_plans import WorkPlans
    from app.integrations.ecs_callback.binding import DeviceBinding
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW, compile_commands
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
    from app.integrations.ecs_command.service import CommandOwner, EcsCommands
    from app.models import IntegrationExchange

    host = direction_host
    host.entry.COMPONENTS = (COMPONENT, replace(ROLLER, entry_modules=ROLLER.entry_modules[:1]))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(events, "_registry", {})
    name = ROLLER.entry_modules[0]
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)
    source = events.lookup("SCAN2", "SCAN_COMPLETED").registration_source
    from app.businesses.coarse_sorting.models import (
        CoarseSortingAction,
        CoarseSortingJob,
        RackPreparation,
    )

    metadata = MetaData(schema=Base.metadata.schema)
    tables = {key: table.to_metadata(metadata) for key, table in ROLLER_TABLES.items()}
    for model in (CoarseSortingJob, CoarseSortingAction, RackPreparation):
        model.__table__.to_metadata(metadata)
    for model in (IntegrationExchange,):
        model.__table__.to_metadata(metadata)
    async with host.factory.kw["bind"].begin() as connection:
        copy_history_tables(metadata)
        await connection.run_sync(metadata.create_all)
    target = TargetConfig("ecs", None, PROTOCOL, address_mode="SNAPSHOT")
    _, annexes = compile_commands(MoveForward, MoveLeft, MoveRight)

    class Routes:
        async def resolve_declared(self, session, device_code, declaration):
            binding = declaration.bindings[0]
            return RouteSnapshot(
                device_code,
                "http://ecs.test",
                "ecs",
                "1",
                binding.annex_id,
                binding.annex_version,
                (binding.task_type,),
                PROTOCOL_RETRY_WINDOW,
            )

        async def authorize_frozen(self, session, route):
            return RouteAuthorization.ALLOW

    outbox = Outbox({"ecs": target}, {PROTOCOL: EcsCommandAdapter()}, records=history_records)
    ecs = EcsCommands(outbox, "ecs", Routes(), annexes)
    lifecycle = EcsLifecycle(ecs)
    outbox.lifecycles = {PROTOCOL: lifecycle}
    work_plans = WorkPlans(host.requests, host.dispatch)
    ports = RollerPorts(work_plans=work_plans)
    roller = BinRoller(
        CommandOwner("smt_auto"),
        Capabilities(ecs, host.requests, None, host.factory),
        ports,
        rack_dispatch=host.dispatch,
        tables=tables,
    )
    runtime = BusinessRuntime({}, {"smt_auto": SimpleNamespace(components={"bin_roller": roller})})

    async def binding(session, device_code):
        return DeviceBinding(
            device_id=1,
            device_code=device_code,
            device_version=1,
            work_line_id=host.line_id,
            work_line_version=1,
            device_role=device_code,
            device_bindings={device_code: device_code},
            position_bindings={},
            business_code="smt_auto",
        )

    async def scan(*, timestamp=10, bin_code="A000000001-C", point="SCAN2"):
        async with host.factory.begin() as session:
            result = await receive_ecs_event(
                session,
                json.dumps(
                    {
                        "device_code": point,
                        "event_type": "SCAN_COMPLETED",
                        "timestamp": timestamp,
                        "data": {"bin_code": bin_code},
                    }
                ).encode(),
                resolve_binding=binding,
                resolve_input_target=lambda business, key: InputTarget("bin_roller", source),
                runtime=runtime,
            )
            assert result.status_code == 200
            return await session.scalar(
                select(AutoBinWork).where(
                    AutoBinWork.raw_bin_code == bin_code, AutoBinWork.scanned_at == timestamp
                )
            )

    async def result(command_code, *, status="SUCCESS", device="SCAN2"):
        store = MessageStore(
            host.factory, lifecycles={PROTOCOL: lifecycle}, records=history_records
        )
        for claim in await store.claim(100, {"ecs": target}):
            assert await store.before_send(claim, target)
        body = dict(
            command_code=command_code, device_code=device, result=status, finish_time=100, data={}
        )
        if status == "FAILED":
            body["error_detail"] = {"code": "STOPPED", "msg": "stopped"}
        async with host.factory.begin() as session:
            return await receive_ecs_result(session, json.dumps(body).encode(), ecs)

    host.line_id = 1
    host.roller, host.work_plans, host.scan, host.result = roller, work_plans, scan, result
    yield host
