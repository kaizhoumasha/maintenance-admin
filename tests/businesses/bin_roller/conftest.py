from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_database_engine


@pytest.fixture
async def roller_lifecycle(roller_return, monkeypatch):
    import importlib
    import sys
    from dataclasses import replace

    from app.businesses import catalog
    from app.integrations.wms_request import operation
    from app.models import Device, DeviceRoute, WorkLine

    flow = roller_return
    modules = {
        "outbound_bin_inbound_batch_v1": ("outbound.bin.inbound_batch@v1", "InboundBatch"),
        "rack_departure_decide_v1": ("rack.departure_decide@v1", "DepartureRequest"),
        "workline_return_buffer_drain_rack_decide_v1": (
            "workline.return_buffer.drain_rack_decide@v1",
            "DrainRequest",
        ),
    }
    prefix = "app.businesses._components.bin_roller.request."
    for entry in catalog.entries():
        entry.COMPONENTS = tuple(
            replace(c, entry_modules=(*c.entry_modules, *(prefix + m for m in modules)))
            for c in entry.COMPONENTS
        )
    monkeypatch.setattr(catalog, "_validated_entries", None)
    for name, (op, model) in modules.items():
        module = prefix + name
        if module in sys.modules:
            operation.request_operation(op, operation.Endpoint.DECISIONS)(
                getattr(sys.modules[module], model)
            )
        else:
            importlib.import_module(module)
    catalog.validate_business_contracts()
    metadata = MetaData(schema=WorkLine.__table__.schema)
    for model in (WorkLine, DeviceRoute, Device):
        model.__table__.to_metadata(metadata)
    engine = flow.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.insert().values(
                id=1,
                line_code="LINE-1",
                line_name="Test",
                business_code="first",
                position_bindings=dict(FIVE_RACK="RACK-WORK", INLET="INLET", OUTLET="OUTLET"),
            )
        )
    import json

    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError

    from app.businesses._components.bin_roller.ports import (
        CurrentTask,
        RollerWorkRef,
        SourceFaceRef,
        SuccessorContext,
        SupplyChange,
    )
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive
    from app.models import CallbackReceipt, ReliableMessage, TransportTask

    receipts = set()
    flow.current_task = "task-original"
    flow.successor = "UNKNOWN"

    async def current(session, *, work_line_id):
        return CurrentTask(flow.current_task, flow.current_task is not None)

    async def successor(session, *, work_line_id):
        return SuccessorContext(flow.successor, {})

    flow.production.read_current_task = current
    flow.production.read_successor = successor

    async def advance(kind, record_id, *, before=None):
        for attempt in range(3):
            try:
                async with flow.factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    if before:
                        await before(session, attempt)
                    return await flow.roller.advance(session, RollerWorkRef(kind, record_id))
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def demand(*, rack="R1", face="A", revision=1, task=None):
        async with flow.factory.begin() as session:
            await flow.roller.apply_supply_change(
                session,
                SupplyChange(
                    1,
                    task or flow.current_task,
                    str(uuid7()),
                    (SourceFaceRef(revision, rack, face),),
                    (),
                ),
            )
            table = flow.store.tables["supply_demand"]
            return (
                (
                    await session.execute(
                        select(table).where(
                            table.c.rack_id == rack,
                            table.c.rack_face == face,
                            table.c.plan_revision == revision,
                            table.c.task_id == (task or flow.current_task),
                        )
                    )
                )
                .mappings()
                .one()["id"]
            )

    async def all_rows(key):
        async with flow.factory() as session:
            return await flow.rows(session, key)

    async def decision(key, record_id, data):
        async with flow.factory.begin() as session:
            row = await flow.roller.original_record(session, key, record_id)
            await session.execute(
                ReliableMessage.__table__.update()
                .where(ReliableMessage.id == row["message_id"])
                .values(
                    status="DONE",
                    last_http_status=200,
                    last_verdict="DELIVERED",
                    response_raw=json.dumps(
                        dict(
                            operation_id=row["operation_id"], code="DECIDED", timestamp=2, data=data
                        )
                    ).encode(),
                )
            )

    async def callback(data, operation="transport.task.resulted@v1"):
        op_id = str(uuid7())
        receipts.add(op_id)
        async with flow.factory.begin() as session:
            result = await receive(
                session,
                json.dumps(
                    dict(operation=operation, operation_id=op_id, timestamp=3, data=data)
                ).encode(),
            )
            assert result.status_code in {200, 202}, result

    async def rack_result(action, *, status="SUCCEEDED", revision=1):
        cycle = next(c for c in await all_rows("rack_cycle") if c["id"] == action["cycle_id"])
        data = dict(
            transport_task_id=action["transport_task_id"],
            rack_id=cycle["rack_id"],
            kind="RACK_ROTATE" if action["kind"] == "CTU02" else "RACK_MOVE",
            outcome_revision=revision,
            status=status,
            final_position=action["target"],
            arrival_face=action["target_face"],
        )
        if status == "FAILED":
            data["failure_code"] = "RCS_EXECUTION_FAILED"
        await callback(data)

    async def accepted(action, *, code="RECEIVED", http=202):
        async with flow.factory.begin() as session:
            await session.execute(
                ReliableMessage.__table__.update()
                .where(ReliableMessage.operation_id == action["operation_id"])
                .values(
                    status="DONE",
                    last_http_status=http,
                    last_verdict="DELIVERED",
                    response_raw=json.dumps(
                        dict(
                            operation_id=action["operation_id"],
                            code=code,
                            timestamp=2,
                            data=dict(transport_task_id=action["transport_task_id"]),
                        )
                    ).encode(),
                )
            )

    async def feed_success(segment):
        feed = next(f for f in await all_rows("feed") if f["id"] == segment["feed_id"])
        await callback(
            dict(
                transport_task_id=segment["transport_task_id"],
                kind="BIN_MOVE",
                outcome_revision=1,
                results=[
                    dict(
                        container_id=m["bin_code"],
                        status="SUCCEEDED",
                        final_position=feed["target"],
                    )
                    for m in segment["members"]
                ],
            )
        )

    flow.advance, flow.demand, flow.all_rows, flow.decision = advance, demand, all_rows, decision
    flow.rack_result, flow.accepted, flow.feed_success = rack_result, accepted, feed_success
    flow.rack_callback = callback

    async def ready_supply(*, count=5, rack="R1", face="A", revision=1):
        demand_id = await demand(rack=rack, face=face, revision=revision)
        await advance("FEED", demand_id)
        action = (await all_rows("rack_action"))[-1]
        await rack_result(action)
        await advance("RACK_CYCLE", action["cycle_id"])
        feed = (await all_rows("feed"))[-1]
        await decision(
            "feed",
            feed["id"],
            dict(
                result="READY",
                bins=[
                    dict(
                        bin_code=f"A{100 + i:09}",
                        source_locator=dict(
                            type="RACK_BIN_SLOT", rack_id=rack, rack_face=face, slot_id=f"S{i}"
                        ),
                    )
                    for i in range(count)
                ],
            ),
        )
        await advance("FEED", demand_id)
        return demand_id, action["cycle_id"]

    async def complete_return(*, count=None, no_batch=False):
        batch = next(b for b in reversed(await all_rows("return_batch")) if b["pending"])
        opportunity = next(
            o for o in await all_rows("return_opportunity") if o["id"] == batch["opportunity_id"]
        )
        service = next(
            s for s in await all_rows("face_service") if s["id"] == opportunity["service_id"]
        )
        cycle = next(c for c in await all_rows("rack_cycle") if c["id"] == service["cycle_id"])
        data = (
            dict(result="NO_BATCH")
            if no_batch
            else dict(
                result="READY",
                moves=[
                    dict(
                        sequence_no=i,
                        bin_code=m["bin_code"],
                        target=dict(
                            type="RACK_BIN_SLOT",
                            rack_id=cycle["rack_id"],
                            rack_face=service["rack_face"],
                            slot_id=f"S{i}",
                        ),
                    )
                    for i, m in enumerate(batch["members"][:count], 1)
                ],
            )
        )
        await decision("return_batch", batch["id"], data)
        await advance("RETURN_BATCH", batch["id"])
        if not no_batch:
            await flow.success(batch["id"])
            await advance("RETURN_BATCH", batch["id"])
        return batch["id"]

    async def finish_departure(cycle_id):
        departure = next(
            d for d in reversed(await all_rows("departure")) if d["cycle_id"] == cycle_id
        )
        await decision(
            "departure",
            departure["id"],
            dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
        )
        await advance("RACK_CYCLE", cycle_id)
        action = next(
            a
            for a in await all_rows("rack_action")
            if a["cycle_id"] == cycle_id and a["kind"] == "CTU03"
        )
        await accepted(action)
        await advance("RACK_CYCLE", cycle_id)
        return action

    flow.ready_supply, flow.complete_return, flow.finish_departure = (
        ready_supply,
        complete_return,
        finish_departure,
    )
    try:
        yield flow
    finally:
        from sqlalchemy import delete, select

        async with flow.factory.begin() as session:
            actions = await flow.rows(session, "rack_action")
            task_ids = [a["transport_task_id"] for a in actions if a["transport_task_id"]]
            task_ids += [
                s["transport_task_id"]
                for s in await flow.rows(session, "feed_segment")
                if s["transport_task_id"]
            ]
            operation_ids = select(TransportTask.operation_id).where(
                TransportTask.transport_task_id.in_(task_ids)
            )
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.operation_id.in_(operation_ids))
            )
            await session.execute(
                delete(TransportTask).where(TransportTask.transport_task_id.in_(task_ids))
            )
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.operation_id.in_(receipts))
            )
            await session.execute(delete(WorkLine).where(WorkLine.id == 1))


@pytest.fixture
async def roller_feed(roller_transport, monkeypatch):
    """Selected ready face plus actual request/outbox/Transport ports, no rack scheduler."""
    import importlib
    import json
    import sys

    from sqlalchemy import select

    from app.businesses import catalog
    from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
    from app.businesses._components.bin_roller.service import BinRoller
    from app.businesses.components import ComponentDefinition
    from app.businesses.definitions import BusinessDefinition
    from app.businesses.runtime import Capabilities
    from app.core.identifiers import uuid7
    from app.integrations.ecs_command.service import CommandOwner
    from app.integrations.wms_callback import receive
    from app.integrations.wms_request import WmsRequests, operation
    from app.integrations.wms_request.adapter import ADAPTERS
    from app.models import CallbackReceipt, ReliableMessage, TransportTask
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig
    from tests.helpers import business_composition

    store = roller_transport.store
    monkeypatch.setattr(
        operation,
        "_registry",
        {
            name: item
            for name, item in operation._registry.items()
            if name != "outbound.bin.inbound_batch@v1"
        },
    )
    module = "app.businesses._components.bin_roller.request.outbound_bin_inbound_batch_v1"
    entry = business_composition("first")
    entry.DEFINITION = BusinessDefinition(code="first", display_name="first")
    entry.ENTRY_MODULES = ()
    entry.COMPONENTS = (
        ComponentDefinition(
            key="bin_roller",
            entry_modules=(module,),
        ),
    )
    original_entries = catalog.entries()
    monkeypatch.setattr(catalog, "entries", lambda: (*original_entries, entry))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    if module in sys.modules:
        operation.request_operation("outbound.bin.inbound_batch@v1", operation.Endpoint.DECISIONS)(
            sys.modules[module].InboundBatch
        )
    else:
        importlib.import_module(module)
    catalog.validate_business_contracts()
    target = TargetConfig("feed-" + str(uuid7()), "http://example.test", "wms-decisions")
    wms = WmsRequests(
        Outbox({target.name: target}, ADAPTERS),
        owner_domain="first",
        contract_consumers=catalog.contract_consumers,
    )
    roller = BinRoller(
        CommandOwner("first"),
        Capabilities(None, wms, roller_transport.service, store.factory),
        SimpleNamespace(),
        tables=store.tables,
    )
    receipts = set()
    from app.businesses._components.bin_roller.feed_flow import advance_feed

    async def rows(session, key):
        return (await session.execute(select(store.tables[key]))).mappings().all()

    async def demand(session, *, task="T1", revision=1, change="M1", face="A"):
        await roller.apply_supply_change(
            session, SupplyChange(1, task, change, (SourceFaceRef(revision, "R1", face),), ())
        )
        return next(
            r
            for r in await rows(session, "supply_demand")
            if r["task_id"] == task and r["plan_revision"] == revision and r["rack_face"] == face
        )

    async def ready(session, original, *, cycle_id=None, in_progress=True):
        repo = roller.repository(1)
        if cycle_id is None:
            cycle = await repo.create_cycle(
                session,
                cycle_id=str(uuid7()),
                rack_id="R1",
                purpose="ORDINARY",
                task_id=original["task_id"],
                source_ref=original["id"],
            )
            cycle_id = cycle["id"]
        if in_progress:
            await repo.register_rack_action(
                session,
                cycle_id=cycle_id,
                kind="CTU01",
                source_ref=cycle_id,
                action_id=str(uuid7()),
                client_request_id=str(uuid7()),
                transport_task_id=None,
                operation_id=None,
                target={"kind": "RACK_POSITION", "location_code": "CTU01"},
                target_face=original["rack_face"],
                caller_workline_id="LINE-ORIGINAL",
            )
        service_id = str(uuid7())
        await session.execute(
            store.tables["face_service"]
            .insert()
            .values(
                id=service_id,
                owner_domain="first",
                work_line_id=1,
                cycle_id=cycle_id,
                source_ref=original["id"],
                rack_face=original["rack_face"],
                task_id=original["task_id"],
                plan_revision=original["plan_revision"],
                status="READY",
            )
        )
        await repo.bind_supply(
            session, demand_id=original["id"], cycle_id=cycle_id, service_id=service_id
        )
        return service_id, cycle_id

    async def start(session, original, *, inlet="INLET-ORIGINAL"):
        from app.businesses._components.bin_roller.feed_flow import start_feed
        from app.integrations.wms_transport import HandoffPosition, TransportCaller

        return await start_feed(
            roller,
            session,
            demand_id=original["id"],
            caller=TransportCaller("LINE-ORIGINAL"),
            target=HandoffPosition(inlet),
        )

    async def decide(session, feed_id, *, count=5, data=None):
        feed = next(r for r in await rows(session, "feed") if r["id"] == feed_id)
        data = (
            data
            if data is not None
            else {
                "result": "READY",
                "bins": [
                    {
                        "bin_code": f"A{i:09}",
                        "source_locator": {
                            "type": "RACK_BIN_SLOT",
                            "rack_id": "R1",
                            "rack_face": "A",
                            "slot_id": f"S{i}",
                        },
                    }
                    for i in range(1, count + 1)
                ],
            }
        )
        await session.execute(
            ReliableMessage.__table__.update()
            .where(ReliableMessage.id == feed["message_id"])
            .values(
                status="DONE",
                last_http_status=200,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(operation_id=feed["operation_id"], code="DECIDED", timestamp=2, data=data)
                ).encode(),
            )
        )

    async def success(segment, *, status="SUCCEEDED", revision=1):
        operation_id = str(uuid7())
        receipts.add(operation_id)
        results = [
            dict(
                container_id=m["bin_code"],
                status="FAILED" if status == "UNKNOWN" else status,
                final_position=None
                if status == "UNKNOWN"
                else dict(kind="HANDOFF_POSITION", location_code="INLET-ORIGINAL"),
                **(
                    {"failure_code": "POSITION_UNKNOWN", "position_unknown": True}
                    if status == "UNKNOWN"
                    else {"failure_code": "RCS_EXECUTION_FAILED"}
                    if status == "FAILED"
                    else {}
                ),
            )
            for m in segment["members"]
        ]
        raw = json.dumps(
            dict(
                operation="transport.task.resulted@v1",
                operation_id=operation_id,
                timestamp=3,
                data=dict(
                    transport_task_id=segment["transport_task_id"],
                    kind="BIN_MOVE",
                    outcome_revision=revision,
                    results=results,
                ),
            )
        ).encode()
        async with store.factory.begin() as session:
            response = await receive(session, raw)
            assert response.status_code in {200, 202}, response

    try:
        yield SimpleNamespace(
            store=store,
            roller=roller,
            consume_feed=partial(advance_feed, roller),
            rows=rows,
            demand=demand,
            ready=ready,
            start=start,
            decide=decide,
            success=success,
        )
    finally:
        from sqlalchemy import delete

        async with store.factory.begin() as session:
            segment_ids = [
                r["transport_task_id"]
                for r in await rows(session, "feed_segment")
                if r["transport_task_id"]
            ]
            operations = (
                await session.scalars(
                    select(TransportTask.operation_id).where(
                        TransportTask.transport_task_id.in_(segment_ids)
                    )
                )
            ).all()
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.operation_id.in_(receipts))
            )
            await session.execute(
                delete(ReliableMessage).where(
                    (ReliableMessage.target == target.name)
                    | ReliableMessage.operation_id.in_(operations)
                )
            )
            await session.execute(
                delete(TransportTask).where(TransportTask.transport_task_id.in_(segment_ids))
            )


@pytest.fixture
async def roller_scans(roller_store, monkeypatch):
    """Real receiver/command ports; a test-owned production persists only injected decisions."""
    import importlib
    import json
    import sys

    from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, delete, select

    from app.businesses import catalog
    from app.businesses._components.bin_roller.ecs_command import MoveForward, MoveLeft, MoveRight
    from app.businesses._components.bin_roller.ports import CurrentTask, SuccessorContext
    from app.businesses._components.bin_roller.service import BinRoller
    from app.businesses.catalog import InputTarget
    from app.businesses.components import ComponentDefinition
    from app.businesses.definitions import BusinessDefinition, DeviceRole, InitialEntry
    from app.businesses.runtime import BusinessRuntime, Capabilities
    from app.core.identifiers import uuid7
    from app.integrations.ecs_callback import registry
    from app.integrations.ecs_callback.binding import DeviceBinding
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command import PROTOCOL_RETRY_WINDOW, compile_commands
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.integrations.ecs_command.routing import RouteAuthorization, RouteSnapshot
    from app.integrations.ecs_command.service import CommandOwner, EcsCommands
    from app.models import (
        CallbackReceipt,
        EcsCommand,
        EcsResultEvidence,
        EcsSendEvidence,
        ReliableMessage,
    )
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig
    from app.reliable.store import MessageStore
    from tests.helpers import business_composition

    monkeypatch.setattr(registry, "_registry", {})
    module_name = "app.businesses._components.bin_roller.ecs_event.scan_completed"
    component = ComponentDefinition(
        key="bin_roller",
        device_roles=tuple(
            DeviceRole(role_key=p, display_name=p) for p in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")
        ),
        initial_entries=tuple(
            InitialEntry(kind="ecs_event", name="SCAN_COMPLETED", device_role=p)
            for p in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")
        ),
        entry_modules=(module_name,),
        commands=(MoveForward, MoveLeft, MoveRight),
    )
    entries = []
    for code in ("first", "second"):
        entry = business_composition(code)
        entry.DEFINITION = BusinessDefinition(code=code, display_name=code)
        entry.ENTRY_MODULES = ()
        entry.COMPONENTS = (component,)
        entries.append(entry)
    monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])
    else:
        importlib.import_module(module_name)
    source = registry.lookup("SCAN1", "SCAN_COMPLETED").registration_source
    metadata = MetaData()
    production_table = Table(
        "test_roller_production",
        metadata,
        Column("arrival_id", String, primary_key=True),
        Column("business", String, nullable=False),
        Column("work_line_id", Integer, nullable=False),
        Column("raw_bin_code", String, nullable=False),
        Column("scanned_at", BigInteger, nullable=False),
        Column("decision", String, nullable=False),
        Column("release_requested", Boolean, nullable=False),
    )
    models = (CallbackReceipt, EcsCommand, EcsResultEvidence, EcsSendEvidence, ReliableMessage)
    for model in models:
        model.__table__.to_metadata(metadata)
    store = roller_store
    engine = store.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    prefix = "roller-" + str(uuid7())
    devices = {point: prefix + "-" + point for point in ("SCAN1", "SCAN2", "SCAN3", "SCAN4")}
    target = TargetConfig(prefix, None, PROTOCOL, address_mode="SNAPSHOT")
    _, annexes = compile_commands(MoveForward, MoveLeft, MoveRight)

    class Routes:
        async def resolve_declared(self, session, device_code, declaration):
            binding = declaration.bindings[0]
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

    outbox = Outbox({prefix: target}, {PROTOCOL: EcsCommandAdapter()})
    ecs = EcsCommands(outbox, prefix, Routes(), annexes)
    lifecycle = EcsLifecycle(ecs)
    outbox.lifecycles = {PROTOCOL: lifecycle}
    capabilities = Capabilities(ecs, None, None, store.factory)

    class Production:
        current_valid = True
        decision = "WAIT"
        release_requested = False
        fail = False

        async def read_current_task(self, session, *, work_line_id):
            return CurrentTask("task-original", self.current_valid)

        async def read_successor(self, session, *, work_line_id):
            return SuccessorContext("UNKNOWN", {})

        async def accept_scan2(self, ctx, arrival):
            facts = await rows(ctx.session, "scan_fact")
            assert any(f["arrival_id"] == arrival.arrival_id for f in facts)
            if self.fail:
                raise RuntimeError("production storage failure")
            await ctx.session.execute(
                production_table.insert().values(
                    arrival_id=arrival.arrival_id,
                    business=ctx.business_code,
                    work_line_id=arrival.work_line_id,
                    raw_bin_code=arrival.raw_bin_code,
                    scanned_at=arrival.scanned_at,
                    decision=self.decision,
                    release_requested=self.release_requested,
                )
            )
            if self.release_requested:
                await flow.roller.release_scan2(ctx.session, arrival_id=arrival.arrival_id)

    production = Production()
    flow = SimpleNamespace(
        store=store,
        factory=store.factory,
        devices=devices,
        production=production,
        work_line_id=1,
        ecs=ecs,
        target=target,
        production_table=production_table,
    )

    def rebuild(owner="first"):
        flow.roller = BinRoller(CommandOwner(owner), capabilities, production, tables=store.tables)
        flow.runtime = BusinessRuntime(
            {}, {owner: SimpleNamespace(components={"bin_roller": flow.roller})}
        )

    async def binding(session, device_code):
        point = next(point for point, code in devices.items() if code == device_code)
        return DeviceBinding(
            device_id=1,
            device_code=device_code,
            device_version=1,
            work_line_id=flow.work_line_id,
            work_line_version=1,
            device_role=point,
            device_bindings=devices,
            position_bindings={},
            business_code=flow.roller.context.domain,
        )

    async def scan(point, raw, *, timestamp=1, session=None):
        body = json.dumps(
            dict(
                device_code=devices[point],
                event_type="SCAN_COMPLETED",
                timestamp=timestamp,
                data={"bin_code": raw},
            )
        ).encode()
        if session is None:
            async with store.factory.begin() as current:
                return await scan(point, raw, timestamp=timestamp, session=current)
        return await receive_ecs_event(
            session,
            body,
            resolve_binding=binding,
            resolve_input_target=lambda business, key: InputTarget("bin_roller", source),
            runtime=flow.runtime,
        )

    async def rows(session, key):
        table = store.tables[key]
        return (await session.execute(select(table).order_by(table.c.id))).mappings().all()

    async def messages(session):
        return (
            await session.scalars(select(ReliableMessage).where(ReliableMessage.target == prefix))
        ).all()

    async def command(session, command_code):
        return await session.get(EcsCommand, command_code)

    async def result(code, point, *, status="SUCCESS", finish_time=100):
        message_store = MessageStore(store.factory, lifecycles={PROTOCOL: lifecycle})
        for claim in await message_store.claim(100, {prefix: target}):
            assert await message_store.before_send(claim, target)
        body = dict(
            command_code=code,
            device_code=devices[point],
            result=status,
            finish_time=finish_time,
            data={},
        )
        if status == "FAILED":
            body["error_detail"] = {"code": "STOPPED", "msg": "stopped"}
        async with store.factory.begin() as session:
            response = await receive_ecs_result(session, json.dumps(body).encode(), ecs)
            assert response.status_code == 200, response
        return response

    rebuild()
    flow.rebuild, flow.scan, flow.rows = rebuild, scan, rows
    flow.messages, flow.command, flow.result = messages, command, result
    try:
        yield flow
    finally:
        async with store.factory.begin() as session:
            codes = select(EcsCommand.command_code).where(EcsCommand.target == prefix)
            await session.execute(
                delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
            )
            await session.execute(
                delete(EcsSendEvidence).where(EcsSendEvidence.command_code.in_(codes))
            )
            await session.execute(delete(EcsCommand).where(EcsCommand.target == prefix))
            await session.execute(delete(ReliableMessage).where(ReliableMessage.target == prefix))
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.device_code.in_(devices.values()))
            )
        async with engine.begin() as connection:
            await connection.run_sync(production_table.drop)
            if engine.dialect.name == "sqlite":
                for model in reversed(models):
                    await connection.run_sync(model.__table__.drop)


@pytest.fixture(params=["sqlite", "postgres"])
async def roller_store(request):
    from app.businesses._components.bin_roller.models import TABLES
    from app.businesses._components.bin_roller.repository import RollerRepository

    url = "sqlite+aiosqlite://"
    if request.param == "postgres":
        url = request.getfixturevalue("pg_url")
    engine = create_database_engine(url)
    metadata = MetaData()
    tables = {key: table.to_metadata(metadata) for key, table in TABLES.items()}
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        yield SimpleNamespace(
            factory=async_sessionmaker(engine, expire_on_commit=False),
            repo=RollerRepository("first", 1, tables=tables),
            tables=tables,
        )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def roller_transport(roller_store):
    import json

    from sqlalchemy import delete

    from app.businesses._components.bin_roller.repository import RollerRepository
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive
    from app.integrations.wms_transport import (
        ADAPTERS,
        PROTOCOL,
        Rack,
        RackPosition,
        TransportCaller,
        TransportService,
    )
    from app.models import CallbackReceipt, ReliableMessage, TransportTask
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    store = roller_store
    engine = store.factory.kw["bind"]
    metadata = MetaData()
    for table in [ReliableMessage.__table__, TransportTask.__table__, CallbackReceipt.__table__]:
        table.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    target = TargetConfig("roller-" + str(uuid7()), "http://example.test", PROTOCOL)
    service = TransportService(
        Outbox({target.name: target}, ADAPTERS), target=target.name, owner_domain="first"
    )
    caller = TransportCaller("1")
    task_ids, operation_ids = set(), set()

    async def create_action(session, *, cycle_id, kind, source_ref, target_face):
        repository = RollerRepository("first", 1, tables=store.tables)
        action = await repository.register_rack_action(
            session,
            cycle_id=cycle_id,
            kind=kind,
            source_ref=source_ref,
            action_id=str(uuid7()),
            client_request_id=str(uuid7()),
            transport_task_id=None,
            operation_id=None,
            target={"kind": "RACK_POSITION", "location_code": "CTU01"},
            target_face=target_face,
            caller_workline_id=caller.workline_id,
        )
        args = dict(
            client_request_id=action["client_request_id"],
            caller=caller,
            rack_id="R1",
            target_face=target_face,
            cycle_id=cycle_id,
        )
        if kind == "CTU01":
            handle = await service.move_rack(
                session, **args, source=Rack("R1"), target=RackPosition("CTU01")
            )
        else:
            handle = await service.rotate_rack(
                session, **args, position=RackPosition("CTU01"), rcs_template_id="CK04"
            )
        task_ids.add(handle.transport_task_id)
        view = await service.get_result(
            session,
            transport_task_id=handle.transport_task_id,
            client_request_id=handle.client_request_id,
            caller=caller,
        )
        await repository.bind_transport(
            session,
            action_id=action["id"],
            transport_task_id=handle.transport_task_id,
            operation_id=view.operation_id,
        )
        return await repository.get_action(session, action["id"])

    async def read_result(session, action):
        return await service.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=caller,
        )

    async def callback(
        action, *, revision=1, status="SUCCEEDED", location="CTU01", face="A", operation_id=None
    ):
        operation_id = operation_id or str(uuid7())
        operation_ids.add(operation_id)
        data = dict(
            transport_task_id=action["transport_task_id"],
            kind="RACK_MOVE",
            rack_id="R1",
            outcome_revision=revision,
            status=status,
            final_position=dict(kind="RACK_POSITION", location_code=location)
            if location is not None
            else None,
            arrival_face=face,
        )
        if status == "FAILED":
            data["failure_code"] = (
                "POSITION_UNKNOWN" if location is None else "RCS_EXECUTION_FAILED"
            )
            if location is None:
                data["position_unknown"] = True
        raw = json.dumps(
            dict(
                operation="transport.task.resulted@v1",
                operation_id=operation_id,
                timestamp=1,
                data=data,
            )
        ).encode()
        async with store.factory.begin() as session:
            response = await receive(session, raw)
            assert response.status_code in {200, 202}, response
        return operation_id

    try:
        yield SimpleNamespace(
            store=store,
            create_action=create_action,
            read_result=read_result,
            callback=callback,
            target=target.name,
            service=service,
        )
    finally:
        async with store.factory.begin() as session:
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.operation_id.in_(operation_ids))
            )
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.target == target.name)
            )
            await session.execute(
                delete(TransportTask).where(TransportTask.transport_task_id.in_(task_ids))
            )
        if engine.dialect.name == "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)


@pytest.fixture
async def roller_return(roller_scans, monkeypatch):
    """Real scans/commands, WMS allocation and Transport with a selected lifecycle input."""
    import importlib
    import json
    import sys
    from dataclasses import replace

    from sqlalchemy import delete, select

    from app.businesses import catalog
    from app.businesses.runtime import Capabilities
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive
    from app.integrations.wms_request import WmsRequests, operation
    from app.integrations.wms_request.adapter import ADAPTERS
    from app.integrations.wms_transport import (
        ADAPTERS as TRANSPORT_ADAPTERS,
    )
    from app.integrations.wms_transport import (
        PROTOCOL,
        HandoffPosition,
        TransportCaller,
        TransportService,
    )
    from app.models import CallbackReceipt, ReliableMessage, TransportTask
    from app.reliable import Outbox
    from app.reliable.config import TargetConfig

    flow, store = roller_scans, roller_scans.store
    engine = store.factory.kw["bind"]
    metadata = MetaData()
    TransportTask.__table__.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    transport_target = TargetConfig(
        "return-transport-" + str(uuid7()), "http://example.test", PROTOCOL
    )
    transport = TransportService(
        Outbox({transport_target.name: transport_target}, TRANSPORT_ADAPTERS),
        target=transport_target.name,
        owner_domain="first",
    )
    module = "app.businesses._components.bin_roller.request.outbound_bin_return_batch_v1"
    op = "outbound.bin.return_batch@v1"
    monkeypatch.setattr(operation, "_registry", {})
    for entry in catalog.entries():
        entry.COMPONENTS = tuple(
            replace(c, entry_modules=(*c.entry_modules, module)) for c in entry.COMPONENTS
        )
    monkeypatch.setattr(catalog, "_validated_entries", None)
    if module in sys.modules:
        operation.request_operation(op, operation.Endpoint.DECISIONS)(
            sys.modules[module].ReturnBatchRequest
        )
    else:
        importlib.import_module(module)
    catalog.validate_business_contracts()
    target = TargetConfig("return-" + str(uuid7()), "http://example.test", "wms-decisions")
    wms = WmsRequests(
        Outbox({target.name: target}, ADAPTERS),
        owner_domain="first",
        contract_consumers=catalog.contract_consumers,
    )
    flow.roller.capabilities = Capabilities(flow.ecs, wms, transport, store.factory)
    from app.businesses._components.bin_roller.return_flow import advance_return_batch

    flow.consume_return = partial(advance_return_batch, flow.roller)
    receipts = set()

    async def selected(session, *, kind="FACE_END", face="A", line=1):
        repo = flow.roller.repository(line)
        cycle = await repo.create_cycle(
            session,
            cycle_id=str(uuid7()),
            rack_id="R1",
            purpose="ORDINARY",
            task_id="T1",
            source_ref=str(uuid7()),
        )
        service_id, opportunity_id = str(uuid7()), str(uuid7())
        await session.execute(
            store.tables["face_service"]
            .insert()
            .values(
                id=service_id,
                **repo.scope,
                cycle_id=cycle["id"],
                source_ref=opportunity_id,
                rack_face=face,
                status="READY",
            )
        )
        await session.execute(
            store.tables["return_opportunity"]
            .insert()
            .values(
                id=opportunity_id,
                **repo.scope,
                service_id=service_id,
                source_ref=service_id,
                kind=kind,
            )
        )
        return opportunity_id

    async def bins(count=2, *, offset=0):
        for i in range(offset + 1, offset + count + 1):
            await flow.scan("SCAN3", f"A{i:09}-B", timestamp=i * 10)
            await flow.scan("SCAN4", f"A{i:09}-B", timestamp=i * 10 + 1)
            async with store.factory() as session:
                arrivals = [
                    a
                    for a in await flow.rows(session, "scan_arrival")
                    if a["point"] == "SCAN4" and a["bin_code"] == f"A{i:09}"
                ]
            await flow.result(arrivals[-1]["command_code"], "SCAN4", finish_time=i * 10 + 2)

    async def start(session, opportunity_id, *, source_ref=None, outlet="OUTLET-ORIGINAL"):
        return await flow.roller.start_return_batch(
            session,
            opportunity_id=opportunity_id,
            source_ref=source_ref or opportunity_id,
            workline_code="KT16",
            caller=TransportCaller("LINE-ORIGINAL"),
            source=HandoffPosition(outlet),
        )

    async def decide(session, batch_id, *, count=None, data=None):
        batch = await flow.roller.original_record(session, "return_batch", batch_id)
        moves = [
            dict(
                sequence_no=i,
                bin_code=m["bin_code"],
                target=dict(type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id=f"S{i}"),
            )
            for i, m in enumerate(batch["members"][:count], 1)
        ]
        data = data if data is not None else dict(result="READY", moves=moves)
        await session.execute(
            ReliableMessage.__table__.update()
            .where(ReliableMessage.id == batch["message_id"])
            .values(
                status="DONE",
                last_http_status=200,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(operation_id=batch["operation_id"], code="DECIDED", timestamp=2, data=data)
                ).encode(),
            )
        )
        return data

    async def callback(data, operation_name="transport.task.resulted@v1"):
        operation_id = str(uuid7())
        receipts.add(operation_id)
        async with store.factory.begin() as session:
            response = await receive(
                session,
                json.dumps(
                    dict(
                        operation=operation_name, operation_id=operation_id, timestamp=3, data=data
                    )
                ).encode(),
            )
            assert response.status_code in {200, 202}, response

    async def success(batch_id, *, placed=True, revision=1, status="SUCCEEDED"):
        async with store.factory() as session:
            batch = await flow.roller.original_record(session, "return_batch", batch_id)
        results = []
        for move in batch["manifest"]["moves"]:
            position = dict(move["target"])
            position["kind"] = position.pop("type")
            if placed:
                await callback(
                    dict(
                        transport_task_id=batch["transport_task_id"],
                        container_id=move["bin_code"],
                        milestone="TARGET_PLACED",
                        final_position=position,
                    ),
                    "transport.task.member_position_changed@v1",
                )
            results.append(
                dict(
                    container_id=move["bin_code"],
                    status="FAILED" if status == "UNKNOWN" else status,
                    final_position=None if status == "UNKNOWN" else position,
                    **(
                        dict(position_unknown=True, failure_code="POSITION_UNKNOWN")
                        if status == "UNKNOWN"
                        else dict(failure_code="RCS_EXECUTION_FAILED")
                        if status == "FAILED"
                        else {}
                    ),
                )
            )
        await callback(
            dict(
                transport_task_id=batch["transport_task_id"],
                kind="BIN_MOVE",
                outcome_revision=revision,
                results=results,
            )
        )

    flow.selected, flow.bins, flow.start, flow.decide, flow.success = (
        selected,
        bins,
        start,
        decide,
        success,
    )
    try:
        yield flow
    finally:
        async with store.factory.begin() as session:
            batches = await flow.rows(session, "return_batch")
            task_ids = [b["transport_task_id"] for b in batches if b["transport_task_id"]]
            operations = (
                await session.scalars(
                    select(TransportTask.operation_id).where(
                        TransportTask.transport_task_id.in_(task_ids)
                    )
                )
            ).all()
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.operation_id.in_(receipts))
            )
            await session.execute(
                delete(ReliableMessage).where(
                    (ReliableMessage.target == target.name)
                    | ReliableMessage.operation_id.in_(operations)
                )
            )
            await session.execute(
                delete(TransportTask).where(TransportTask.transport_task_id.in_(task_ids))
            )


@pytest.fixture
def roller_declaration(monkeypatch):
    import importlib
    import sys

    from app.businesses import catalog
    from app.businesses._components.bin_roller import composition
    from app.businesses.definitions import BusinessDefinition, DeviceRole
    from app.integrations.ecs_callback import registry as events
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_request import operation as requests
    from tests.helpers import business_composition

    assert hasattr(composition, "COMPONENT"), "bin_roller must export its actual declaration"
    component = composition.COMPONENT
    entries = []
    for code in ("first", "second"):
        entry = business_composition(code)
        entry.DEFINITION = BusinessDefinition(
            code=code,
            display_name=code,
            device_roles=(DeviceRole(role_key="PRODUCTION", display_name="Production"),),
        )
        entry.ENTRY_MODULES = ()
        entry.COMPONENTS = (component,)
        entry.PROGRESSION = "scan_pending"
        entries.append(entry)
    monkeypatch.setattr(catalog, "entries", lambda: tuple(entries))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(events, "_registry", {})
    monkeypatch.setattr(callbacks, "_registry", {})
    monkeypatch.setattr(requests, "_registry", {})
    request_models = {
        "outbound_bin_inbound_batch_v1": "InboundBatch",
        "outbound_bin_return_batch_v1": "ReturnBatchRequest",
        "workline_return_buffer_drain_rack_decide_v1": "DrainRequest",
        "rack_departure_decide_v1": "DepartureRequest",
    }
    for name in component.entry_modules:
        if name not in sys.modules:
            importlib.import_module(name)
        elif name.rsplit(".", 1)[-1] in request_models:
            module = sys.modules[name]
            requests.request_operation(module.OPERATION, requests.Endpoint.DECISIONS)(
                getattr(module, request_models[name.rsplit(".", 1)[-1]])
            )
        else:
            importlib.reload(sys.modules[name])
    return component, entries


@pytest.fixture
async def roller_application(roller_scans, request):
    """Test consumers exercise real assembly/receiver; no application business is installed."""
    import json

    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from app.businesses import catalog
    from app.businesses._components.bin_roller.ports import CurrentTask, SuccessorContext
    from app.businesses._components.bin_roller.service import BinRoller
    from app.businesses.runtime import build_runtime
    from app.integrations.ecs_callback.binding import DeviceBinding
    from app.integrations.ecs_callback.receiver import receive_ecs_event
    from app.integrations.ecs_command.service import CommandOwner

    flow = roller_scans
    _, entries = request.getfixturevalue("roller_declaration")
    table = flow.production_table
    kinds = ("SCAN", "SCAN2_RELEASE", "FEED", "RETURN_BATCH", "RACK_CYCLE", "DRAIN")

    class Production:
        def __init__(self, owner):
            self.owner = owner
            self.valid = True
            self.permit = False

        async def read_current_task(self, session, *, work_line_id):
            return CurrentTask("task-" + self.owner, self.valid)

        async def read_successor(self, session, *, work_line_id):
            return SuccessorContext("UNKNOWN", {})

        async def accept_scan2(self, ctx, arrival):
            assert await ctx.session.scalar(
                select(flow.store.tables["scan_fact"].c.id).where(
                    flow.store.tables["scan_fact"].c.arrival_id == arrival.arrival_id
                )
            )
            await ctx.session.execute(
                table.insert().values(
                    arrival_id=arrival.arrival_id,
                    business=self.owner,
                    work_line_id=arrival.work_line_id,
                    raw_bin_code=arrival.raw_bin_code,
                    scanned_at=arrival.scanned_at,
                    decision="PERMIT" if self.permit else "WAIT",
                    release_requested=self.permit,
                )
            )
            if self.permit:
                await (
                    ctx.runtime.services[self.owner]
                    .components["bin_roller"]
                    .release_scan2(ctx.session, arrival_id=arrival.arrival_id)
                )

    class Host:
        def __init__(self, owner, capabilities):
            self.capabilities = capabilities
            self.production = Production(owner)
            self.components = {
                "bin_roller": BinRoller(
                    CommandOwner(owner), capabilities, self.production, tables=flow.store.tables
                )
            }
            self.attempts = []
            self.visited = []
            self.failures = []

        async def advance(self, work, *, before=None):
            for attempt in range(3):
                try:
                    async with self.capabilities.session_factory() as session, session.begin():
                        self.attempts.append(session)
                        if session.get_bind().dialect.name == "postgresql":
                            await session.connection(
                                execution_options={"isolation_level": "SERIALIZABLE"}
                            )
                        if before is not None:
                            await before(session, attempt)
                        return await self.components["bin_roller"].advance(session, work)
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                        raise

        async def scan_pending(self, *, limit=1):
            roller = self.components["bin_roller"]
            async with self.capabilities.session_factory() as session:
                upper = {
                    kind: await roller.get_pending_upper_id(session, kind=kind) for kind in kinds
                }
            cursors = {kind: None for kind in kinds if upper[kind] is not None}
            total = 0
            while cursors:
                for kind in tuple(cursors):
                    async with self.capabilities.session_factory() as session:
                        page = await roller.list_pending(
                            session,
                            kind=kind,
                            after_id=cursors[kind],
                            upper_id=upper[kind],
                            limit=limit,
                        )
                    if not page:
                        del cursors[kind]
                        continue
                    for work in page:
                        self.visited.append(work)
                        try:
                            await self.advance(work)
                        except Exception as error:
                            self.failures.append((work, error))
                        total += 1
                    cursors[kind] = page[-1].record_id
            return total

        async def complete_production(self, arrival_id):
            async with self.capabilities.session_factory() as session, session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    await session.connection(
                        execution_options={"isolation_level": "REPEATABLE READ"}
                    )
                original = (
                    (
                        await session.execute(
                            select(table).where(
                                table.c.arrival_id == arrival_id,
                                table.c.business == self.production.owner,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if not original["release_requested"]:
                    await self.components["bin_roller"].release_scan2(
                        session, arrival_id=arrival_id
                    )
                    await session.execute(
                        update(table)
                        .where(table.c.arrival_id == arrival_id)
                        .values(decision="COMPLETE", release_requested=True)
                    )

    for entry in entries:
        entry.build_service = partial(Host, entry.DEFINITION.code)
    catalog.validate_business_contracts()

    def rebuild():
        flow.runtime = build_runtime(
            outbox=flow.ecs.outbox, ecs_commands=flow.ecs, session_factory=flow.factory
        )
        flow.hosts = flow.runtime.services

    notifications = []

    async def scan(owner, point, raw, *, timestamp=1, session=None, notify=None):
        if session is None:
            async with flow.factory() as current, current.begin():
                if current.get_bind().dialect.name == "postgresql":
                    await current.connection(
                        execution_options={"isolation_level": "REPEATABLE READ"}
                    )
                return await scan(
                    owner, point, raw, timestamp=timestamp, session=current, notify=notify
                )

        async def binding(session, device_code):
            return DeviceBinding(
                device_id=1,
                device_code=device_code,
                device_version=1,
                work_line_id=1 if owner == "first" else 2,
                work_line_version=1,
                device_role=point,
                device_bindings=flow.devices,
                position_bindings={},
                business_code=owner,
            )

        return await receive_ecs_event(
            session,
            json.dumps(
                dict(
                    device_code=flow.devices[point],
                    event_type="SCAN_COMPLETED",
                    timestamp=timestamp,
                    data={"bin_code": raw},
                )
            ).encode(),
            resolve_binding=binding,
            runtime=flow.runtime,
            resolve_input_target=catalog.input_target,
            on_business_committed=notifications.append if notify is None else notify,
        )

    rebuild()
    flow.application_scan = scan
    flow.application_rebuild = rebuild
    flow.notifications = notifications
    return flow
