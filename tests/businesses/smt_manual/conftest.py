import importlib
import json
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, case, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.database import create_database_engine
from app.integrations import history as history_records
from app.models import (
    Device,
    DeviceRoute,
    IntegrationExchange,
    ReliableMessage,
    WorkLine,
    WorkLinePosition,
)
from tests.helpers import clear_integration_history, set_message_bodies, update_message_bodies


@pytest.fixture
async def manual_host(manual_bins, manual_racks, monkeypatch):
    from dataclasses import replace

    from app.businesses._components.bin_roller import service as roller_service
    from app.businesses.smt_manual import repository
    from app.businesses.smt_manual.service import SmtManualService

    flow = manual_racks
    monkeypatch.setattr(repository, "TABLES", flow.tables)
    monkeypatch.setattr(roller_service, "TABLES", flow.roller_tables)
    flow.host = SmtManualService(replace(flow.racks.capabilities, ecs=flow.manual.capabilities.ecs))
    flow.runtime.services["smt_manual"] = flow.host
    return flow


@pytest.fixture
async def manual_sessions(request, tmp_path, postgres_tables):
    url = (
        f"sqlite+aiosqlite:///{tmp_path / 'manual.db'}"
        if getattr(request, "param", "postgres") == "sqlite"
        else request.getfixturevalue("pg_url")
    )
    from app.businesses._components.rack_dispatch.models import RackAction, RackJob
    from app.database import SCHEMA
    from app.models import IntegrationExchange, TransportTask

    metadata = MetaData(schema=SCHEMA)
    for model in (
        RackJob,
        RackAction,
        WorkLine,
        WorkLinePosition,
        DeviceRoute,
        Device,
        IntegrationExchange,
        ReliableMessage,
        TransportTask,
    ):
        model.__table__.to_metadata(metadata)
    from tests.helpers import copy_history_tables

    copy_history_tables(metadata)
    async with AsyncExitStack() as stack:
        postgres = url.startswith("postgres")
        if postgres:
            engine = await stack.enter_async_context(postgres_tables(url, "manual", metadata))
        else:
            engine = create_database_engine(url)
            stack.push_async_callback(engine.dispose)
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all, checkfirst=False)
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def manual_flow(manual_sessions, monkeypatch):
    from app.businesses import catalog
    from app.businesses._components.bin_roller.composition import COMPONENT as ROLLER
    from app.businesses._components.bin_roller.service import BinRoller
    from app.businesses._components.sorting_task.composition import COMPONENT as TASK
    from app.businesses._components.sorting_task.service import SortingTask
    from app.businesses.definitions import BusinessDefinition, PositionSlot
    from app.businesses.runtime import BusinessRuntime, Capabilities
    from app.businesses.smt_manual.models import TABLES
    from app.businesses.smt_manual.repository import TaskRepository
    from app.businesses.smt_manual.roller_ports import RollerPorts
    from app.integrations import discovery
    from app.integrations.ecs_callback import registry as events
    from app.integrations.ecs_command.service import CommandOwner
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_callback.receiver import receive
    from app.integrations.wms_request import WmsRequests, operation
    from app.integrations.wms_request.adapter import ADAPTERS
    from app.reliable.config import TargetConfig
    from app.reliable.outbox import Outbox
    from tests.helpers import business_composition

    metadata = MetaData()

    def clone(tables, prefix):
        result = {}
        for key, table in tables.items():
            result[key] = table.to_metadata(metadata, name=prefix + key, schema=None)
            for number, constraint in enumerate(result[key].constraints):
                constraint.name = f"{prefix}{key}_constraint_{number}"
            for index in result[key].indexes:
                index.name = prefix + index.name
        return result

    tables = clone(TABLES, "test_smt_manual_")
    from tests.helpers import copy_roller_tables

    roller_tables = copy_roller_tables(metadata, "test_smt_roller_")
    for table in roller_tables.values():
        for number, constraint in enumerate(table.constraints):
            constraint.name = f"{table.name}_constraint_{number}"
        for index in table.indexes:
            index.name = "test_smt_roller_" + index.name
    engine = manual_sessions.kw["bind"]
    entry = business_composition("smt_manual")
    entry.DEFINITION = BusinessDefinition(
        code="smt_manual",
        display_name="SMT manual",
        position_slots=(
            PositionSlot(
                position_type="RACK_POSITION",
                slot_key="RETURN_RACK",
                display_name="Direct",
                required=False,
            ),
            PositionSlot(
                position_type="RACK_POSITION",
                slot_key="TRANSFER_RACK",
                display_name="Transfer",
                required=False,
            ),
        ),
    )
    from app.businesses._components.rack_dispatch.composition import COMPONENT as DISPATCH

    entry.COMPONENTS = (TASK, ROLLER, DISPATCH)
    entry.ENTRY_MODULES = ()
    entry.PROGRESSION = "scan_pending"
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(catalog, "_registration_guard_enabled", False)
    monkeypatch.setattr(callbacks, "_registry", {})
    monkeypatch.setattr(events, "_registry", {})
    monkeypatch.setattr(operation, "_registry", {})
    monkeypatch.setattr(discovery, "validate_late_registration", lambda source: None)
    modules = (*TASK.entry_modules, ROLLER.entry_modules[0])
    original_modules = {name: sys.modules[name] for name in modules if name in sys.modules}
    for name in modules:
        if ".request." in name and name in sys.modules:
            model = "Prepare" if name.endswith("prepare_v1") else "CompletionConfirm"
            op = (
                "sorting.task.prepare@v1"
                if model == "Prepare"
                else "sorting.task.completion_confirm@v1"
            )
            operation.request_operation(op, operation.Endpoint.DECISIONS)(
                getattr(sys.modules[name], model)
            )
        elif name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    from app.businesses.smt_manual.task_flow import SmtManualTaskFlow

    catalog.validate_business_contracts()
    target = TargetConfig("smt-m1", "http://example.test", "wms-decisions")
    wms = WmsRequests(
        Outbox({target.name: target}, ADAPTERS, records=history_records),
        owner_domain="smt_manual",
        contract_consumers=catalog.contract_consumers,
    )
    capabilities = Capabilities(None, wms, None, manual_sessions)
    repository = TaskRepository(tables=tables)
    ports = RollerPorts(repository)
    from app.businesses._components.rack_dispatch.service import RackDispatch

    roller = BinRoller(
        CommandOwner("smt_manual"),
        capabilities,
        ports,
        rack_dispatch=RackDispatch("smt_manual", capabilities, task_admission=ports),
        tables=roller_tables,
    )
    task = SmtManualTaskFlow(capabilities, roller, repository=repository)
    runtime = BusinessRuntime(
        definitions={"smt_manual": entry.DEFINITION},
        services={
            "smt_manual": SimpleNamespace(
                components={
                    "sorting_task": SortingTask("smt_manual", task),
                    "bin_roller": roller,
                    "rack_dispatch": roller.rack_dispatch,
                }
            )
        },
    )
    lines, operations, messages, notices = [], set(), set(), []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with manual_sessions.begin() as session:
            for number in (1, 2):
                line = WorkLine(
                    line_code=f"SMT-M1-{number}",
                    line_name="Manual",
                    business_code="smt_manual",
                    position_bindings=dict(
                        FIVE_RACK="FIVE",
                        INLET="IN",
                        OUTLET="OUT",
                        RETURN_RACK="RETURN",
                        TRANSFER_RACK="TRANSFER",
                    ),
                )
                session.add(line)
                await session.flush()
                if number == 2:
                    line.position_bindings = {
                        k: v + "-2" for k, v in line.position_bindings.items()
                    }
                from tests.helpers import configure_rack_positions

                await configure_rack_positions(session, line)
                lines.append(line.id)

        def message(name, data, *, operation_id=None, timestamp=100):
            operation_id = operation_id or str(uuid7())
            operations.add(operation_id)
            return json.dumps(
                dict(
                    operation=f"sorting.task.{name}@v1",
                    operation_id=operation_id,
                    timestamp=timestamp,
                    data=data,
                )
            ).encode()

        async def deliver(raw, *, rollback=False, before_commit=None):
            async with manual_sessions() as session:
                response = await receive(
                    session, raw, runtime=runtime, on_business_committed=notices.append
                )
                if before_commit:
                    await before_commit()
                if rollback:
                    await session.rollback()
                else:
                    await session.commit()
                return response

        async def issue(task_id="T1", direction="outbound", line=1, **values):
            data = dict(
                task_id=task_id,
                task_type="MANUAL",
                business_type=direction,
                workline_code=f"SMT-M1-{line}",
                queue_revision=1,
                dispatch_sequence=100,
            )
            data.update(values)
            return await deliver(message("issued", data))

        async def rows(key, *, component=False):
            from tests.helpers import bin_roller_rows, roller_dispatch_rows, smt_manual_query

            if component and key in ("rack_cycle", "rack_action", "departure"):
                async with manual_sessions() as session:
                    return [
                        r
                        for line in lines
                        for r in await roller_dispatch_rows(session, roller, key, line)
                    ]
            if not component and key in {"rack", "rack_action", "rack_departure"}:
                from tests.helpers import manual_dispatch_rows

                async with manual_sessions() as session:
                    return await manual_dispatch_rows(session, roller.rack_dispatch, key, lines)
            if component:
                async with manual_sessions() as session:
                    return await bin_roller_rows(session, roller, key)
            query = smt_manual_query(tables, key)
            async with manual_sessions() as session:
                return (await session.execute(query)).mappings().all()

        async def prepare(line=1, *, now_ms=1000, rollback=False):
            async with manual_sessions() as session:
                selected = await task.prepare_next(
                    session, work_line_id=lines[line - 1], now_ms=now_ms
                )
                if rollback:
                    await session.rollback()
                else:
                    await session.commit()
                messages.update(
                    r["prepare_operation_id"]
                    for r in await rows("task")
                    if r["prepare_operation_id"]
                )
                return selected

        async def accept_prepare(task_id="T1"):
            row = next(row for row in await rows("task") if row["task_id"] == task_id)
            async with manual_sessions.begin() as session:
                await update_message_bodies(
                    session,
                    update(ReliableMessage).where(ReliableMessage.id == row["prepare_message_id"]),
                    status=case((ReliableMessage.status == "ABANDONED", "ABANDONED"), else_="DONE"),
                    last_http_status=202,
                    last_verdict="DELIVERED",
                    response_raw=json.dumps(
                        dict(
                            operation_id=row["prepare_operation_id"],
                            code="PREPARE_ACCEPTED",
                            timestamp=110,
                            data={},
                        )
                    ).encode(),
                )
            async with manual_sessions.begin() as session:
                if engine.dialect.name == "postgresql":
                    await session.connection(
                        execution_options={"isolation_level": "REPEATABLE READ"}
                    )
                return await task.advance_task(session, task_id=task_id)

        async def ready(task_id="T1", direction="outbound", line=1):
            assert (await issue(task_id, direction, line)).status_code == 202
            assert await prepare(line) == task_id
            await accept_prepare(task_id)

        yield SimpleNamespace(
            factory=manual_sessions,
            task=task,
            ports=ports,
            roller=roller,
            tables=tables,
            roller_tables=roller_tables,
            lines=lines,
            runtime=runtime,
            message=message,
            deliver=deliver,
            issue=issue,
            rows=rows,
            prepare=prepare,
            accept_prepare=accept_prepare,
            ready=ready,
            notices=notices,
        )
    finally:
        async with manual_sessions.begin() as session:
            await session.execute(
                update(IntegrationExchange)
                .where(
                    IntegrationExchange.role == "RECEIPT",
                    IntegrationExchange.receipt_active.is_(True),
                )
                .values(receipt_active=False)
                .where(IntegrationExchange.operation_id.in_(operations))
            )
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.business_key == "business:smt_manual")
            )
            await session.execute(
                delete(WorkLinePosition).where(WorkLinePosition.work_line_id.in_(lines))
            )
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(lines)))
        if engine.dialect.name == "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)
        for name in modules:
            if name in original_modules:
                sys.modules[name] = original_modules[name]
            else:
                sys.modules.pop(name, None)


@pytest.fixture
async def manual_bins(manual_flow, monkeypatch):
    """Actual receiver, WMS outbox and roller with the manual production port."""
    from dataclasses import replace

    from sqlalchemy import delete
    from sqlalchemy.exc import DBAPIError

    from app.businesses import catalog
    from app.businesses.smt_manual.manual_bin_flow import ManualBinFlow
    from app.integrations.ecs_callback.binding import DeviceBinding
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW, compile_commands
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
    from app.integrations.ecs_command.service import EcsCommands
    from app.integrations.wms_request import operation
    from app.models import IntegrationExchange
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig
    from app.reliable.store import MessageStore

    flow = manual_flow
    entry = catalog.entries()[0]
    prefix = "manual-" + str(uuid7())
    engine = flow.factory.kw["bind"]
    devices = {point: prefix + "-" + point for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")}
    route_ids = []
    async with flow.factory.begin() as session:
        for code in devices.values():
            route_id = str(uuid7())
            route_ids.append(route_id)
            session.add(DeviceRoute(id=route_id, device_code=code, ecs_origin="http://ecs.test"))
            await session.flush()
            session.add(
                Device(
                    device_code=code,
                    display_name=code,
                    work_line_id=flow.lines[0],
                    current_route_id=route_id,
                )
            )
        line = await session.get(WorkLine, flow.lines[0])
        line.device_bindings = devices
    target = TargetConfig(prefix, None, PROTOCOL, address_mode="SNAPSHOT")

    class Routes:
        async def resolve_declared(self, session, device_code, declaration):
            role = next(point for point, code in devices.items() if code == device_code)
            binding = next(b for b in declaration.bindings if b.device_role == role)
            return RouteSnapshot(
                device_code,
                "http://ecs.test",
                prefix,
                "1",
                binding.annex_id,
                binding.annex_version,
                (binding.task_type,),
                PROTOCOL_RETRY_WINDOW,
            )

        async def authorize_frozen(self, session, route):
            return RouteAuthorization.ALLOW

    outbox = Outbox({prefix: target}, {PROTOCOL: EcsCommandAdapter()}, records=history_records)
    _, annexes = compile_commands(*entry.COMPONENTS[1].commands)
    ecs = EcsCommands(outbox, prefix, Routes(), annexes)
    lifecycle = EcsLifecycle(ecs)
    outbox.lifecycles = {PROTOCOL: lifecycle}
    flow.task.capabilities.wms.outbox.lifecycles.update(outbox.lifecycles)
    capabilities = replace(flow.task.capabilities, ecs=ecs)
    flow.roller.capabilities = capabilities
    modules = (
        "app.businesses.smt_manual.request.manual_bin_work_admission_decide_v1",
        "app.businesses.smt_manual.callback.manual_bin_work_completed_v1",
    )
    entry.ENTRY_MODULES = (*entry.ENTRY_MODULES, *modules)
    monkeypatch.setattr(catalog, "_validated_entries", None)
    for name in modules:
        if name in sys.modules and ".request." in name:
            if "manual_bin.work_admission_decide@v1" not in operation._registry:
                operation.request_operation(
                    "manual_bin.work_admission_decide@v1", operation.Endpoint.DECISIONS
                )(sys.modules[name].AdmissionRequest)
        elif name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    catalog.validate_business_contracts()
    flow.manual = ManualBinFlow(capabilities, flow.roller, repository=flow.task.repository)
    flow.ports.manual_bins = flow.manual
    service = flow.runtime.services["smt_manual"]
    service.read_task_owner = flow.task.read_task_owner
    service.accept_manual_completed = flow.manual.accept_manual_completed

    async def binding(session, device_code):
        role = next(point for point, code in devices.items() if code == device_code)
        return DeviceBinding(
            device_id=1,
            device_code=device_code,
            device_version=1,
            work_line_id=flow.lines[0],
            work_line_version=1,
            device_role=role,
            device_bindings=devices,
            position_bindings={},
            business_code="smt_manual",
        )

    async def scan(
        raw="A000000001-C", *, timestamp=10, rollback=False, before_commit=None, point="SCAN2"
    ):
        body = json.dumps(
            dict(
                device_code=devices[point],
                event_type="SCAN_COMPLETED",
                timestamp=timestamp,
                data={"bin_code": raw},
            )
        ).encode()
        async with flow.factory() as session:
            response = await receive_ecs_event(
                session,
                body,
                resolve_binding=binding,
                resolve_input_target=catalog.input_target,
                runtime=flow.runtime,
            )
            if before_commit:
                await before_commit()
            await session.rollback() if rollback else await session.commit()
            return response

    async def advance(kind, record_id, *, now_ms=1000, rollback=False, before=None):
        for attempt in range(3):
            try:
                async with flow.factory() as session, session.begin():
                    if engine.dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    if before:
                        await before(session, attempt)
                    result = await getattr(flow.manual, "advance_" + kind)(
                        session,
                        **(
                            {"arrival_id": record_id, "now_ms": now_ms}
                            if kind == "bin"
                            else {"operation_id": record_id}
                        ),
                    )
                    if rollback:
                        await session.rollback()
                    return result
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def decision(
        data, *, index=-1, http=200, code="DECIDED", response_id=None, timestamp=100
    ):
        row = (await flow.rows("admission"))[index]
        async with flow.factory.begin() as session:
            await update_message_bodies(
                session,
                update(ReliableMessage).where(ReliableMessage.id == row["message_id"]),
                status="DONE",
                last_http_status=http,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(
                        operation_id=response_id or row["operation_id"],
                        code=code,
                        timestamp=timestamp,
                        data=data,
                    )
                ).encode(),
            )

    def completion(data=None, *, operation_id=None, timestamp=100, **values):
        original = json.loads(
            flow.message("unused", {}, operation_id=operation_id, timestamp=timestamp)
        )
        original["operation"] = "manual_bin.work_completed@v1"
        original["data"] = (
            data
            if data is not None
            else dict(
                task_id="T1", bin_code="A000000001", result="NORMAL", completed_at=50, **values
            )
        )
        return json.dumps(original).encode()

    async def result(command_code, *, point="SCAN2"):
        store = MessageStore(
            flow.factory, lifecycles={PROTOCOL: lifecycle}, records=history_records
        )
        for claim in await store.claim(100, {prefix: target}):
            assert await store.before_send(claim, target)
        async with flow.factory.begin() as session:
            response = await receive_ecs_result(
                session,
                json.dumps(
                    dict(
                        command_code=command_code,
                        device_code=devices[point],
                        result="SUCCESS",
                        finish_time=100,
                        data={},
                    )
                ).encode(),
                ecs,
            )
            assert response.status_code == 200

    async def ready_for_bin_work(*, direction="outbound"):
        await flow.ready(direction=direction)
        if direction == "outbound":
            decision = await flow.deliver(
                flow.message(
                    "plan_delta",
                    dict(
                        task_id="T1",
                        plan_revision=1,
                        added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
                    ),
                )
            )
            assert decision.status_code == 202

    flow.ready_for_bin_work = ready_for_bin_work
    flow.scan, flow.advance, flow.decision = scan, advance, decision
    flow.completion, flow.result = completion, result
    try:
        yield flow
    finally:
        async with flow.factory.begin() as session:
            roots = list(
                await session.scalars(
                    select(IntegrationExchange.id).where(
                        IntegrationExchange.role == "ECS_COMMAND",
                        IntegrationExchange.target == prefix,
                    )
                )
            )
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == prefix))
            await clear_integration_history(session, roots)
            await session.execute(delete(Device).where(Device.device_code.in_(devices.values())))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id.in_(route_ids)))


@pytest.fixture
async def manual_racks(manual_flow, monkeypatch):
    from dataclasses import replace

    from app.businesses import catalog
    from app.businesses.smt_manual.manual_rack_flow import ManualRackFlow
    from app.integrations.wms_request import operation
    from app.integrations.wms_request.adapter import ADAPTERS
    from app.integrations.wms_transport import PROTOCOL, TransportService, TransportSubmitAdapter
    from app.models import TransportTask
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    flow = manual_flow
    entry = catalog.entries()[0]
    modules = (
        "app.businesses.smt_manual.callback.manual_rack_direct_pick_completed_v1",
        "app.businesses.smt_manual.callback.outbound_return_rack_task_completed_v1",
        "app.businesses.smt_manual.request.outbound_return_rack_arrival_report_v1",
    )
    original_modules = {name: sys.modules[name] for name in modules if name in sys.modules}
    entry.ENTRY_MODULES = (*entry.ENTRY_MODULES, *modules)
    monkeypatch.setattr(catalog, "_validated_entries", None)
    for name in modules:
        if name in sys.modules and ".request." in name:
            operation.request_operation(
                "outbound.return_rack.arrival_report@v1", operation.Endpoint.FACTS
            )(sys.modules[name].ArrivalReport)
        elif name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    name = "app.businesses._components.rack_dispatch.request.rack_departure_decide_v1"
    if name in sys.modules:
        operation.request_operation("rack.departure_decide@v1", operation.Endpoint.DECISIONS)(
            sys.modules[name].DepartureRequest
        )
    else:
        importlib.import_module(name)
    name = "app.integrations.wms_transport.callback.transport_task_resulted_v1"
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)
    catalog.validate_business_contracts()
    prefix = "m3-" + str(uuid7())
    target = TargetConfig(prefix, "http://transport.test", PROTOCOL)
    transport = TransportService(
        Outbox({prefix: target}, {PROTOCOL: TransportSubmitAdapter()}, records=history_records),
        target=prefix,
        owner_domain="smt_manual",
    )
    wms = flow.task.capabilities.wms
    facts = TargetConfig(prefix + "-facts", "http://facts.test", "wms-facts")
    wms._outbox = Outbox(
        {**wms.outbox.targets, facts.name: facts},
        ADAPTERS,
        lifecycles=wms.outbox.lifecycles,
        records=history_records,
    )
    capabilities = replace(flow.task.capabilities, transport=transport)
    flow.task.capabilities = capabilities
    flow.roller.capabilities = capabilities
    flow.roller.rack_dispatch.capabilities = capabilities
    flow.racks = ManualRackFlow(capabilities, flow.roller, repository=flow.task.repository)
    flow.task.manual_racks = flow.racks
    service = flow.runtime.services["smt_manual"]
    service.read_task_owner = flow.task.read_task_owner
    service.accept_direct_pick_completed = flow.racks.accept_direct_pick_completed
    service.accept_return_rack_completed = flow.racks.accept_return_rack_completed

    def notification(*, face=None, revision=1, task_id="T1", rack_id="DIRECT", **values):
        raw = json.loads(flow.message("unused", {}))
        raw["operation"] = (
            "manual_rack.direct_pick_completed@v1"
            if face is not None
            else "outbound.return_rack.task_completed@v1"
        )
        raw["data"] = dict(task_id=task_id, rack_id=rack_id, completed_at=50, **values)
        raw["data"].update(
            dict(plan_revision=revision, rack_face=face)
            if face is not None
            else dict(through_plan_revision=revision)
        )
        return json.dumps(raw).encode()

    async def plan(revision=1, faces=("A",), target=None, *, task_id="T1"):
        data = dict(task_id=task_id, plan_revision=revision)
        if faces:
            data["added_direct_picks"] = [
                dict(
                    source_locator=dict(
                        type="RACK_SLOT", rack_id="DIRECT", rack_face=f, slot_id=f"S{revision}"
                    )
                )
                for f in faces
            ]
        if target:
            data["target_rack"] = target
        return await flow.deliver(flow.message("plan_delta", data))

    async def reconcile(task_id="T1"):
        async with flow.factory.begin() as session:
            await flow.racks.reconcile_task(session, task_id=task_id)

    async def advance(rack_id=None, *, now_ms=1000, rollback=False):
        rack_id = rack_id or (await flow.rows("rack"))[0]["id"]
        async with flow.factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            job = await flow.racks.dispatch.progress(session, job_id=rack_id)
            await flow.task.repository.lock_dispatch_line(
                session, flow.roller, job.origin.work_line_id, task_id=job.origin.task_id
            )
            state = await flow.racks.advance_rack(session, rack_work_id=rack_id, now_ms=now_ms)
            if rollback:
                await session.rollback()
            return state

    async def outcome(
        action=None, *, location=None, face=None, revision=1, status="SUCCEEDED", unknown=False
    ):
        action = action or (await flow.rows("rack_action"))[-1]
        async with flow.factory() as session:
            original = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == action["transport_task_id"]
                )
            )
            data = dict(
                transport_task_id=original.transport_task_id,
                kind=original.kind,
                outcome_revision=revision,
                rack_id=original.frozen["rack_id"],
                status=status,
            )
            if unknown:
                data.update(position_unknown=True, failure_code="POSITION_UNKNOWN")
            else:
                data["final_position"] = dict(
                    kind="RACK_POSITION",
                    location_code=location or action["target"]["location_code"],
                )
                if face is not None:
                    data["arrival_face"] = face
                if status == "FAILED":
                    data["failure_code"] = "RCS_EXECUTION_FAILED"
        raw = json.loads(flow.message("unused", data))
        raw["operation"] = "transport.task.resulted@v1"
        return await flow.deliver(json.dumps(raw).encode())

    async def response(key, data, *, index=-1, code="DECIDED", http=200):
        row = (await flow.rows(key))[index]
        async with flow.factory.begin() as session:
            await update_message_bodies(
                session,
                update(ReliableMessage).where(ReliableMessage.id == row["message_id"]),
                status="DONE",
                last_http_status=http,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(operation_id=row["operation_id"], code=code, timestamp=100, data=data)
                ).encode(),
            )

    async def acknowledge(action):
        async with flow.factory.begin() as session:
            message = await session.get(ReliableMessage, action["submit_message_id"])
            message.status, message.last_http_status, message.last_verdict = (
                "DONE",
                202,
                "DELIVERED",
            )
            await set_message_bodies(
                session,
                message,
                response_raw=json.dumps(
                    dict(
                        operation_id=message.operation_id,
                        code="RECEIVED",
                        timestamp=100,
                        data=dict(transport_task_id=action["transport_task_id"]),
                    )
                ).encode(),
            )

    async def consume_report():
        (report,) = await flow.rows("rack_report")
        async with flow.factory.begin() as session:
            return await flow.racks.advance_report(session, operation_id=report["operation_id"])

    flow.acknowledge, flow.consume_report = acknowledge, consume_report
    flow.notification, flow.plan, flow.reconcile = notification, plan, reconcile
    flow.rack_advance, flow.rack_outcome, flow.rack_response = advance, outcome, response
    try:
        yield flow
    finally:
        async with flow.factory.begin() as session:
            await session.execute(
                delete(TransportTask).where(
                    TransportTask.operation_id.in_(
                        select(ReliableMessage.operation_id).where(ReliableMessage.target == prefix)
                    )
                )
            )
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == prefix))
        for name in modules:
            if name in original_modules:
                sys.modules[name] = original_modules[name]
            else:
                sys.modules.pop(name, None)


@pytest.fixture
def overlapping_manual_snapshots(manual_bins):
    import asyncio

    barrier = asyncio.Barrier(2)
    attempts = []

    async def before(session, attempt):
        attempts.append(attempt)
        if attempt == 0:
            await session.execute(select(manual_bins.tables["manual_bin"]))
            await barrier.wait()

    return SimpleNamespace(before=before, attempts=attempts)


@pytest.fixture
async def manual_completion_tools(manual_flow, monkeypatch):
    from datetime import UTC, datetime

    from sqlalchemy.exc import DBAPIError

    flow = manual_flow
    clock = [1000]
    monkeypatch.setattr(
        flow.task.capabilities.wms, "clock", lambda: datetime.fromtimestamp(clock[0] / 1000, UTC)
    )

    async def confirm(*, now_ms=1000, task_id="T1", before=None, rollback=False):
        clock[0] = now_ms
        for attempt in range(3):
            try:
                async with flow.factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    if before:
                        await before(session, attempt)
                    result = await flow.task.advance_task(session, task_id=task_id)
                    if rollback:
                        await session.rollback()
                    return result
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def confirmations():
        async with flow.factory() as session:
            return list(
                await session.scalars(
                    select(ReliableMessage)
                    .where(
                        ReliableMessage.business_key == "business:smt_manual",
                        ReliableMessage.operation == "sorting.task.completion_confirm@v1",
                    )
                    .order_by(ReliableMessage.id)
                )
            )

    async def confirmation_response(data, *, http=200, code="DECIDED", timestamp=100, index=-1):
        original = (await confirmations())[index]
        async with flow.factory.begin() as session:
            await update_message_bodies(
                session,
                update(ReliableMessage).where(ReliableMessage.id == original.id),
                status="ABANDONED" if original.status == "ABANDONED" else "DONE",
                last_http_status=http,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(
                        operation_id=original.operation_id,
                        code=code,
                        timestamp=timestamp,
                        data=data,
                    )
                ).encode(),
            )

    async def bin_plan(revision=1, *, cancel=False, **fields):
        response = await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=revision,
                    added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
                    **fields,
                ),
            )
        )
        assert response.status_code == 202
        if cancel:
            response = await flow.deliver(
                flow.message(
                    "cancel",
                    dict(
                        task_id="T1",
                        cancel_scope="PLAN_MEMBERS",
                        bin_source_racks=[
                            dict(plan_revision=revision, rack_id="R", rack_face=["A"])
                        ],
                    ),
                )
            )
            assert response.status_code == 202

    flow.confirm, flow.confirmations = confirm, confirmations
    flow.confirmation_response, flow.bin_plan = confirmation_response, bin_plan
    flow.test_clock = clock
    return flow


@pytest.fixture
async def manual_supply(manual_racks, manual_completion_tools):
    from app.businesses import catalog
    from app.businesses._components.bin_roller.feed_flow import advance_face
    from app.integrations.wms_request import operation

    flow = manual_racks
    module = "app.businesses._components.bin_roller.request.bin_inbound_batch_v1"
    if module in sys.modules:
        if "bin.inbound_batch@v1" not in operation._registry:
            operation.request_operation("bin.inbound_batch@v1", operation.Endpoint.DECISIONS)(
                sys.modules[module].InboundBatch
            )
    else:
        importlib.import_module(module)
    catalog.validate_business_contracts()

    async def started(*, feed=False):
        from app.businesses._components.bin_roller.ports import RollerWorkRef

        original = (await flow.rows("supply_demand", component=True))[0]
        async with flow.factory.begin() as session:
            await flow.roller.advance(session, RollerWorkRef("FACE", original["id"]))
        if feed:
            entry = (await flow.rows("rack_action", component=True))[0]
            await flow.rack_outcome(entry, face=entry["target_face"])
            async with flow.factory.begin() as session:
                await flow.roller.advance(session, RollerWorkRef("RACK_CYCLE", entry["cycle_id"]))
        return original["id"]

    async def withdraw():
        assert (
            await flow.deliver(
                flow.message(
                    "cancel",
                    dict(
                        task_id="T1",
                        cancel_scope="PLAN_MEMBERS",
                        bin_source_racks=[dict(plan_revision=1, rack_id="R", rack_face=["A"])],
                    ),
                )
            )
        ).status_code == 202

    async def advance(demand_id):
        async with flow.factory.begin() as session:
            await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            return await advance_face(flow.roller, session, demand_id)

    async def feed_success(demand_id):
        original = (await flow.rows("feed", component=True))[0]
        async with flow.factory.begin() as session:
            await update_message_bodies(
                session,
                update(ReliableMessage).where(ReliableMessage.id == original["message_id"]),
                status="DONE",
                last_http_status=200,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(
                        operation_id=original["operation_id"],
                        code="DECIDED",
                        timestamp=100,
                        data=dict(
                            result="READY",
                            bins=[
                                dict(
                                    bin_code="A000000001",
                                    source_locator=dict(
                                        type="RACK_BIN_SLOT",
                                        rack_id="R",
                                        rack_face="A",
                                        slot_id="S1",
                                    ),
                                )
                            ],
                        ),
                    )
                ).encode(),
            )
        await advance(demand_id)
        segment = (await flow.rows("feed_segment", component=True))[0]
        raw = json.loads(flow.message("unused", {}))
        raw.update(
            operation="transport.task.resulted@v1",
            data=dict(
                transport_task_id=segment["transport_task_id"],
                kind="BIN_MOVE",
                outcome_revision=1,
                results=[
                    dict(
                        container_id="A000000001",
                        status="SUCCEEDED",
                        final_position=dict(kind="HANDOFF_POSITION", location_code="IN"),
                    )
                ],
            ),
        )
        assert (await flow.deliver(json.dumps(raw).encode())).status_code == 202
        await advance(demand_id)
        await advance(demand_id)

    flow.start_supply, flow.withdraw_supply = started, withdraw
    flow.feed_success, flow.advance_supply = feed_success, advance
    return flow


@pytest.fixture
async def native_manual_sessions(manual_sessions):
    from app.businesses._components.bin_roller.models import TABLES as ROLLER
    from app.businesses.smt_auto.models import AutoTask
    from app.businesses.smt_manual.models import TABLES
    from app.database import SCHEMA
    from app.models import AuditLog

    metadata = MetaData(schema=SCHEMA)
    for table in (*TABLES.values(), *ROLLER.values(), AuditLog.__table__, AutoTask.__table__):
        table.to_metadata(metadata)
    engine = manual_sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield manual_sessions
    finally:
        if engine.dialect.name == "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)


@pytest.fixture
async def formal_manual(native_manual_sessions, monkeypatch):
    from tests.helpers import formal_manual_fixture

    async with formal_manual_fixture(native_manual_sessions, monkeypatch) as state:
        yield state


@pytest.fixture
async def profile_flow(native_manual_sessions, monkeypatch, tmp_path, request):
    from app import business_wakeups
    from tests.businesses.smt_manual.factory_profile import profile_fixture

    async with profile_fixture(
        native_manual_sessions, monkeypatch, tmp_path, request.node.name
    ) as flow:
        monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
        yield flow
