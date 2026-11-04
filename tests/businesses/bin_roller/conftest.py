from contextlib import AsyncExitStack
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.businesses._components.rack_dispatch.contracts import Coverage, FactRef
from app.businesses._components.rack_dispatch.models import RackAction, RackJob
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.database import SCHEMA, create_database_engine
from tests.helpers import bin_roller_rows, roller_dispatch_rows


async def arrived_face(roller, session, cycle_id, service_id, receipts):
    """Drive the selected fixture face through its original Transport callback and projection."""
    import json

    from app.businesses._components.bin_roller.rack_flow import row_fact
    from app.businesses._components.rack_dispatch.contracts import (
        DirectHandoff,
        PointDemand,
        PointRef,
    )
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive

    dispatch = roller.rack_dispatch
    face = await roller.original_record(session, "face_work", service_id)
    repo = roller.repository(face["work_line_id"])
    job = await dispatch.progress(session, job_id=cycle_id)
    if job.position_action_id and job.actual_face == face["rack_face"]:
        action_id = job.position_action_id
    else:
        action_id = await dispatch.prepare(
            session,
            job_id=cycle_id,
            kind="ROTATE" if job.actions else "ENTER",
            source_ref=service_id,
            face=face["rack_face"],
        )
        assert action_id
        move = (await dispatch.progress(session, job_id=cycle_id)).actions[-1]
        original_id = str(uuid7())
        receipts.add(original_id)
        response = await receive(
            session,
            json.dumps(
                dict(
                    operation="transport.task.resulted@v1",
                    operation_id=original_id,
                    timestamp=1,
                    data=dict(
                        transport_task_id=move.transport_task_id,
                        kind="RACK_MOVE" if move.kind == "ENTER" else "RACK_ROTATE",
                        rack_id=job.origin.rack_id,
                        outcome_revision=1,
                        status="SUCCEEDED",
                        final_position=move.target,
                        arrival_face=face["rack_face"],
                    ),
                )
            ).encode(),
        )
        assert response.status_code == 202
        point = PointRef(
            repo.owner_domain, repo.work_line_id, job.id, job.origin.rack_id, job.origin.point
        )
        source = row_fact("face_work", repo.tables["face_work"], face)
        coverage = (
            Coverage(
                "selected_face",
                (point.owner_domain, str(point.work_line_id), job.id, point.rack_id, point.point),
                source,
                (source,),
            ),
        )
        job = await dispatch.advance(
            session,
            job_id=cycle_id,
            demand=PointDemand(point, "CONTINUE", coverage),
            handoff=DirectHandoff(point, "BLOCKED", coverage),
            now_ms=1,
        )
        move = job.actions[-1]
        assert move.phase == "APPLIED" and move.outcome["status"] == "SUCCEEDED"
    return action_id


@pytest.fixture
async def roller_lifecycle(roller_return):
    import json

    from sqlalchemy import select

    from app.businesses._components.bin_roller.ports import (
        CurrentTask,
        RollerWorkRef,
        SourceFaceRef,
        SuccessorContext,
        SupplyChange,
    )
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive
    from app.models import ReliableMessage, WorkLine

    flow = roller_return
    factory = flow.factory
    flow.current_task, flow.successor = "task-original", "UNKNOWN"

    async def current(session, *, work_line_id):
        return CurrentTask(flow.current_task, flow.current_task is not None)

    async def successor(session, *, work_line_id):
        return SuccessorContext(flow.successor, {})

    flow.production.read_current_task, flow.production.read_successor = current, successor
    async with factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.position_bindings = dict(FIVE_RACK="RACK-WORK", INLET="INLET", OUTLET="OUTLET")
        from sqlalchemy import delete

        from app.models import WorkLinePosition
        from tests.helpers import configure_rack_positions

        await session.execute(delete(WorkLinePosition).where(WorkLinePosition.work_line_id == 1))
        await configure_rack_positions(session, line)

    async def advance(kind, record_id, *, before=None):
        from sqlalchemy.exc import DBAPIError

        for attempt in range(3):
            try:
                async with factory() as session, session.begin():
                    if session.get_bind().dialect.name == "postgresql":
                        await session.connection(
                            execution_options={"isolation_level": "SERIALIZABLE"}
                        )
                    if before:
                        await before(session, attempt)
                    await flow.lock_scan_line(session, 1)
                    await flow.roller.lock_jobs_on_line(session, 1)
                    await flow.roller.lock_production_subjects(session, 1, ())
                    return await flow.roller.advance(session, RollerWorkRef(kind, record_id))
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise

    async def demand(*, rack="R1", face="A", revision=1, task=None):
        async with factory.begin() as session:
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
            return next(
                r["id"]
                for r in await flow.rows(session, "face_work")
                if r["scope_id"] == (task or flow.current_task)
                and r["rack_id"] == rack
                and r["rack_face"] == face
                and r["plan_revision"] == revision
            )

    async def all_rows(key):
        async with factory() as session:
            return await flow.rows(session, key)

    async def decision(key, identity, data):
        async with factory.begin() as session:
            row = next(r for r in await flow.rows(session, key) if r["id"] == identity)
            message = await session.get(ReliableMessage, row["message_id"])
            message.status, message.last_http_status, message.last_verdict = (
                "DONE",
                200,
                "DELIVERED",
            )
            message.response_raw = json.dumps(
                dict(operation_id=message.operation_id, code="DECIDED", timestamp=2, data=data)
            ).encode()

    async def callback(data, operation="transport.task.resulted@v1"):
        async with factory.begin() as session:
            response = await receive(
                session,
                json.dumps(
                    dict(operation=operation, operation_id=str(uuid7()), timestamp=3, data=data)
                ).encode(),
            )
            assert response.status_code in (200, 202), response

    async def rack_result(action, *, status="SUCCEEDED", revision=1):
        cycle = next(r for r in await all_rows("rack_cycle") if r["id"] == action["cycle_id"])
        data = dict(
            transport_task_id=action["transport_task_id"],
            kind="RACK_ROTATE" if action["kind"] == "CTU02" else "RACK_MOVE",
            rack_id=cycle["rack_id"],
            outcome_revision=revision,
            status=status,
            final_position=action["target"],
            arrival_face=action["target_face"],
        )
        if status == "FAILED":
            data["failure_code"] = "RCS_EXECUTION_FAILED"
        await callback(data)

    async def accepted(action, *, code="RECEIVED", http=202, response_task=None):
        async with factory.begin() as session:
            message = await session.scalar(
                select(ReliableMessage).where(
                    ReliableMessage.operation_id == action["operation_id"]
                )
            )
            message.status, message.last_http_status, message.last_verdict = (
                "DONE",
                http,
                "DELIVERED",
            )
            message.response_raw = json.dumps(
                dict(
                    operation_id=message.operation_id,
                    code=code,
                    timestamp=2,
                    data=dict(transport_task_id=response_task or action["transport_task_id"]),
                )
            ).encode()

    async def ready_supply(*, count=5, rack="R1", face="A", revision=1):
        identity = await demand(rack=rack, face=face, revision=revision)
        await advance("FACE", identity)
        action = (await all_rows("rack_action"))[-1]
        await rack_result(action)
        await advance("RACK_CYCLE", action["cycle_id"])
        await decision(
            "feed",
            identity,
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
        await advance("FACE", identity)
        return identity, action["cycle_id"]

    async def complete_return(*, count=None, no_batch=False):
        face = next(
            r
            for r in await all_rows("face_work")
            if r["phase"] == "WAIT_RETURN_DECISION" and r["closed_at"] is None
        )
        data = (
            {"result": "NO_BATCH"}
            if no_batch
            else dict(
                result="READY",
                moves=[
                    dict(
                        sequence_no=i,
                        bin_code=m["bin_code"],
                        target=dict(
                            type="RACK_BIN_SLOT",
                            rack_id=face["rack_id"],
                            rack_face=face["rack_face"],
                            slot_id=f"S{i}",
                        ),
                    )
                    for i, m in enumerate(face["return_members"][:count], 1)
                ],
            )
        )
        await decision("return_batch", face["id"], data)
        await advance("FACE", face["id"])
        if not no_batch:
            current = next(r for r in await all_rows("face_work") if r["id"] == face["id"])
            await flow.complete_transport(current["current_transport_task_id"])
            await advance("FACE", face["id"])
        return face["id"]

    async def finish_departure(cycle_id):
        departure = next(
            r for r in reversed(await all_rows("departure")) if r["cycle_id"] == cycle_id
        )
        await decision(
            "departure",
            departure["id"],
            dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
        )
        await advance("RACK_CYCLE", cycle_id)
        action = next(
            r
            for r in await all_rows("rack_action")
            if r["cycle_id"] == cycle_id and r["kind"] == "CTU03"
        )
        await accepted(action)
        await advance("RACK_CYCLE", cycle_id)
        return action

    async def feed_success(segment):
        await flow.complete_transport(segment["transport_task_id"])

    flow.advance, flow.demand, flow.all_rows, flow.decision = advance, demand, all_rows, decision
    flow.rack_result, flow.accepted, flow.rack_callback = rack_result, accepted, callback
    flow.ready_supply, flow.complete_return, flow.finish_departure = (
        ready_supply,
        complete_return,
        finish_departure,
    )
    flow.feed_success = feed_success
    return flow


@pytest.fixture
async def roller_feed(roller_current):
    """Actual selected face and original public allocation/move evidence."""
    import json

    from app.businesses._components.bin_roller.feed_flow import advance_face, start_feed
    from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
    from app.businesses._components.bin_roller.rack_flow import register_job
    from app.models import ReliableMessage

    flow = roller_current
    flow.production.current_task = "T1"
    flow.capabilities = flow.roller.capabilities
    flow.consume_feed = lambda session, demand_id: advance_face(flow.roller, session, demand_id)
    receipts = set()

    async def demand(session, *, task="T1", revision=1, change="M1", face="A"):
        await flow.roller.apply_supply_change(
            session, SupplyChange(1, task, change, (SourceFaceRef(revision, "R1", face),), ())
        )
        return next(
            r
            for r in await flow.rows(session, "supply_demand")
            if r["task_id"] == task and r["plan_revision"] == revision and r["rack_face"] == face
        )

    async def ready(session, original, *, cycle_id=None, in_progress=True):
        repo = flow.roller.repository(1)
        if cycle_id is None:
            cycle_id = await register_job(
                flow.roller,
                session,
                repo,
                "ROLLER_ORDINARY",
                original["id"],
                original["rack_id"],
                original["rack_face"],
                original["task_id"],
                dict(
                    workline_code="LINE",
                    caller_workline_id="LINE-ORIGINAL",
                    station_id="STATION-ORIGINAL",
                    positions={
                        "FIVE_RACK": "FIVE",
                        "INLET": "INLET-ORIGINAL",
                        "OUTLET": "OUTLET-ORIGINAL",
                    },
                ),
            )
        await repo.bind_supply(session, demand_id=original["id"], cycle_id=cycle_id)
        action_id = (
            await arrived_face(flow.roller, session, cycle_id, original["id"], receipts)
            if in_progress
            else None
        )
        if action_id:
            await repo.update(session, "face_work", original["id"], preparation_action_id=action_id)
        return original["id"], cycle_id

    async def start(session, original, *, inlet="INLET-ORIGINAL"):
        return await start_feed(flow.roller, session, demand_id=original["id"])

    async def decide(session, feed_id, *, count=5, data=None):
        face = await flow.roller.original_record(session, "face_work", feed_id)
        manifest = (
            data
            if data is not None
            else dict(
                result="READY",
                bins=[
                    dict(
                        bin_code=f"A{i:09}",
                        source_locator=dict(
                            type="RACK_BIN_SLOT",
                            rack_id=face["rack_id"],
                            rack_face=face["rack_face"],
                            slot_id=f"S{i}",
                        ),
                    )
                    for i in range(1, count + 1)
                ],
            )
        )
        message = await session.get(ReliableMessage, face["feed_message_id"])
        message.status, message.last_http_status, message.last_verdict = "DONE", 200, "DELIVERED"
        message.response_raw = json.dumps(
            dict(operation_id=message.operation_id, code="DECIDED", timestamp=2, data=manifest)
        ).encode()
        await session.flush()
        return manifest

    async def success(segment, *, status="SUCCEEDED", revision=1):
        await flow.complete_transport(
            segment["transport_task_id"], status=status, revision=revision
        )

    flow.demand, flow.ready, flow.start, flow.decide, flow.success = (
        demand,
        ready,
        start,
        decide,
        success,
    )
    return flow


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
    from app.integrations.wms_callback import registry as callbacks
    from app.integrations.wms_request import operation as requests
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
    for registry_ in (callbacks, requests):
        monkeypatch.setattr(
            registry_,
            "_registry",
            {
                name: item
                for name, item in registry_._registry.items()
                if not item.registration_source.module.startswith("app.businesses.")
            },
        )
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
        current_task = "task-original"
        decision = "WAIT"
        release_requested = False
        fail = False

        async def read_task_coverage(self, session, *, point, task_id):
            from sqlalchemy import select

            faces = store.tables["face_work"]
            members = (
                (
                    await session.execute(
                        select(faces).where(
                            faces.c.scope_id == task_id, faces.c.work_line_id == point.work_line_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            return (
                Coverage(
                    "test_original_task",
                    (
                        point.owner_domain,
                        str(point.work_line_id),
                        point.job_id,
                        point.rack_id,
                        point.point,
                        task_id,
                    ),
                    FactRef("task", (task_id,), tuple(str(r["change_id"]) for r in members)),
                    (),
                )
                if members
                else None
            )

        async def read_current_task(self, session, *, work_line_id):
            return CurrentTask(self.current_task, self.current_valid)

        async def read_successor(self, session, *, work_line_id):
            return SuccessorContext("UNKNOWN", {})

        async def resolve_scan2_task(self, session, *, work_line_id):
            return await self.read_current_task(session, work_line_id=work_line_id)

        async def accept_scan2(self, ctx, arrival):
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
            return arrival.arrival_id

    production = Production()
    from tests.helpers import fixture_supply_order, rack_task_admission

    production.read_supply_order = fixture_supply_order(store.tables, owner="first")
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
        flow.roller = BinRoller(
            CommandOwner(owner),
            capabilities,
            production,
            rack_dispatch=RackDispatch(
                owner, capabilities, task_admission=rack_task_admission(production)
            ),
            tables=store.tables,
        )
        flow.runtime = BusinessRuntime(
            {}, {owner: SimpleNamespace(components={"bin_roller": flow.roller})}
        )

    async def binding(session, device_code):
        if hasattr(flow, "lock_scan_line"):
            await flow.lock_scan_line(session, flow.work_line_id)
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
        if key in ("rack_cycle", "rack_action", "departure"):
            return await roller_dispatch_rows(session, flow.roller, key)
        return await bin_roller_rows(session, flow.roller, key)

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

    async def record(session, key, record_id, *, for_update=False):
        physical = {
            "supply_demand": "face_work",
            "face_service": "face_work",
            "feed": "face_work",
            "return_batch": "face_work",
            "return_opportunity": "face_work",
            "scan_arrival": "bin_passage",
            "scan2_release": "bin_passage",
            "return_round": "bin_return",
            "drain": "workline_state",
        }.get(key, key)
        if key in flow.roller.tables:
            return await flow.roller.original_record(session, key, record_id, for_update=for_update)
        if for_update and physical in flow.roller.tables:
            await flow.roller.original_record(session, physical, record_id, for_update=True)
        original = next((row for row in await rows(session, key) if row["id"] == record_id), None)
        if original is None:
            raise ValueError("original current checkpoint is missing")
        return original

    rebuild()
    flow.record = record
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
async def roller_store(request, postgres_tables):
    from app.businesses._components.bin_roller.repository import RollerRepository

    url = "sqlite+aiosqlite://"
    if request.param == "postgres":
        url = request.getfixturevalue("pg_url")
    metadata = MetaData(schema=SCHEMA)
    from tests.helpers import copy_roller_tables

    tables = copy_roller_tables(metadata, "test_bin_roller_")
    for table in tables.values():
        for number, constraint in enumerate(table.constraints):
            constraint.name = f"{table.name}_constraint_{number}"
        for index in table.indexes:
            index.name = "test_bin_roller_" + index.name
    for model in (RackJob, RackAction):
        model.__table__.to_metadata(metadata)
    async with AsyncExitStack() as stack:
        if request.param == "postgres":
            engine = await stack.enter_async_context(postgres_tables(url, "roller", metadata))
            schema = engine.get_execution_options()["schema_translate_map"][SCHEMA]
        else:
            schema = None
            engine = create_database_engine(url)
            stack.push_async_callback(engine.dispose)
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all, checkfirst=False)
        yield SimpleNamespace(
            schema=schema,
            factory=async_sessionmaker(engine, expire_on_commit=False),
            repo=RollerRepository(
                "first", 1, rack_dispatch=RackDispatch("first", None), tables=tables
            ),
            tables=tables,
        )


@pytest.fixture
async def roller_transport(roller_store):

    from app.core.identifiers import uuid7
    from app.integrations.wms_transport import (
        ADAPTERS,
        PROTOCOL,
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
    try:
        yield SimpleNamespace(store=store, target=target.name, service=service)
    finally:
        if engine.dialect.name == "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(metadata.drop_all)


@pytest.fixture
async def roller_return(roller_feed):
    import json

    from app.businesses._components.bin_roller.return_flow import advance_return
    from app.core.identifiers import uuid7
    from app.models import ReliableMessage

    flow = roller_feed
    feed_demand = flow.demand
    flow.consume_return = lambda session, face_id: advance_return(flow.roller, session, face_id)

    async def selected(session, *, kind="FACE_END", face="A", line=1):
        original = await feed_demand(
            session,
            revision=len(await flow.rows(session, "face_work")) + 1,
            change=str(uuid7()),
            face=face,
        )
        await flow.ready(session, original)
        await flow.roller.repository(line).update(
            session,
            "face_work",
            original["id"],
            phase="RETURN_WINDOW",
            window_id=str(uuid7()),
            window_kind=kind,
        )
        return original["id"]

    async def bins(count=2, *, offset=0):
        from app.businesses._components.bin_roller.ports import RollerWorkRef

        for number in range(offset + 1, offset + count + 1):
            await flow.scan("SCAN3", f"A{number:09}-B", timestamp=number * 10)
            await flow.scan("SCAN4", f"A{number:09}-B", timestamp=number * 10 + 1)
            async with flow.factory() as session:
                row = next(
                    r
                    for r in await flow.rows(session, "bin_return")
                    if r["bin_code"] == f"A{number:09}" and r["is_current"]
                )
            await flow.result(row["scan4_command_code"], "SCAN4", finish_time=number * 10 + 2)
            async with flow.factory.begin() as session:
                await flow.roller.advance(session, RollerWorkRef("RETURN", row["id"]))

    async def start(session, face_id, *, source_ref=None, outlet="OUTLET-ORIGINAL"):
        face = await flow.roller.original_record(session, "face_work", face_id)
        if face["closed_at"]:
            return None
        if face["phase"] == "RETURN_WINDOW":
            await advance_return(flow.roller, session, face_id)
        face = await flow.roller.original_record(session, "face_work", face_id)
        return face_id if face["step_id"] else None

    async def decide(session, face_id, *, count=None, data=None):
        face = await flow.roller.original_record(session, "face_work", face_id)
        decision = (
            data
            if data is not None
            else dict(
                result="READY",
                moves=[
                    dict(
                        sequence_no=i,
                        bin_code=m["bin_code"],
                        target=dict(
                            type="RACK_BIN_SLOT",
                            rack_id=face["rack_id"],
                            rack_face=face["rack_face"],
                            slot_id=f"S{i}",
                        ),
                    )
                    for i, m in enumerate(face["return_members"][:count], 1)
                ],
            )
        )
        message = await session.get(ReliableMessage, face["return_message_id"])
        message.status, message.last_http_status, message.last_verdict = "DONE", 200, "DELIVERED"
        message.response_raw = json.dumps(
            dict(operation_id=message.operation_id, code="DECIDED", timestamp=2, data=decision)
        ).encode()
        await session.flush()
        return decision

    async def success(face_id, *, placed=True, revision=1, status="SUCCEEDED"):
        async with flow.factory() as session:
            face = await flow.roller.original_record(session, "face_work", face_id)
        await flow.complete_transport(
            face["current_transport_task_id"], placed=placed, revision=revision, status=status
        )

    flow.selected, flow.bins, flow.start, flow.decide, flow.success = (
        selected,
        bins,
        start,
        decide,
        success,
    )
    return flow


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
    from app.businesses._components.rack_dispatch.composition import COMPONENT as dispatch_component

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
        entry.COMPONENTS = (component, dispatch_component)
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
    for name in (*component.entry_modules, *dispatch_component.entry_modules):
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
    kinds = ("PASSAGE", "SCAN2_RELEASE", "RETURN", "FACE", "RACK_CYCLE", "DRAIN")

    class Production:
        def __init__(self, owner):
            self.owner = owner
            self.valid = True
            self.permit = False

        async def read_current_task(self, session, *, work_line_id):
            return CurrentTask("task-" + self.owner, self.valid)

        async def read_successor(self, session, *, work_line_id):
            return SuccessorContext("UNKNOWN", {})

        async def resolve_scan2_task(self, session, *, work_line_id):
            return await self.read_current_task(session, work_line_id=work_line_id)

        async def accept_scan2(self, ctx, arrival):
            await ctx.session.execute(
                table.insert().values(
                    arrival_id=arrival.arrival_id,
                    business=self.owner,
                    work_line_id=arrival.work_line_id,
                    raw_bin_code=arrival.raw_bin_code,
                    scanned_at=arrival.scanned_at,
                    decision="PERMIT" if self.permit else "WAIT",
                    release_requested=False,
                )
            )
            return arrival.arrival_id

    class Host:
        def __init__(self, owner, capabilities):
            self.capabilities = capabilities
            self.production = Production(owner)
            from tests.helpers import fixture_supply_order

            self.production.read_supply_order = fixture_supply_order(flow.store.tables, owner=owner)
            from tests.helpers import rack_task_admission

            dispatch = RackDispatch(
                owner, capabilities, task_admission=rack_task_admission(self.production)
            )
            self.components = {
                "rack_dispatch": dispatch,
                "bin_roller": BinRoller(
                    CommandOwner(owner),
                    capabilities,
                    self.production,
                    rack_dispatch=dispatch,
                    tables=flow.store.tables,
                ),
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
            if self.production.permit:
                async with self.capabilities.session_factory() as session:
                    admitted = list(
                        await session.scalars(
                            select(table.c.arrival_id).where(
                                table.c.business == self.production.owner,
                                table.c.release_requested.is_(False),
                            )
                        )
                    )
                for identity in admitted:
                    await self.complete_production(identity)
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


@pytest.fixture
async def roller_current(roller_scans, roller_declaration):
    """Four checkpoints with original public ports and disposable peer responses."""
    import importlib
    import json
    import sys
    from dataclasses import replace

    from sqlalchemy import select

    from app.businesses import catalog
    from app.businesses._components.bin_roller.ports import (
        RollerWorkRef,
        SourceFaceRef,
        SupplyChange,
    )
    from app.core.identifiers import uuid7
    from app.integrations.wms_callback import receive
    from app.integrations.wms_request import ADAPTERS as WMS_ADAPTERS
    from app.integrations.wms_request import WmsRequests
    from app.integrations.wms_transport import ADAPTERS as TRANSPORT_ADAPTERS
    from app.integrations.wms_transport import TransportService
    from app.models import Device, DeviceRoute, ReliableMessage, TransportTask, WorkLine
    from app.reliable.config import TargetConfig

    module_names = (
        "app.integrations.wms_transport.callback.transport_task_resulted_v1",
        "app.integrations.wms_transport.callback.transport_task_member_position_changed_v1",
    )
    for name in module_names:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    catalog.validate_business_contracts()
    flow = roller_scans
    outbox = flow.ecs.outbox
    for protocol in ("wms-decisions", "wms-facts", "wms-transport"):
        outbox.targets[protocol] = TargetConfig(protocol, "http://peer.test", protocol)
    outbox.adapters.update(WMS_ADAPTERS)
    outbox.adapters.update(TRANSPORT_ADAPTERS)
    wms = WmsRequests(outbox, owner_domain="first", contract_consumers=catalog.contract_consumers)
    transport = TransportService(outbox, target="wms-transport", owner_domain="first")
    capabilities = replace(flow.roller.capabilities, wms=wms, transport=transport)
    from tests.helpers import configure_rack_positions, rack_task_admission

    dispatch = RackDispatch(
        "first", capabilities, task_admission=rack_task_admission(flow.production)
    )
    flow.roller.capabilities = capabilities
    flow.roller.rack_dispatch = dispatch
    metadata = MetaData(schema=SCHEMA)
    from app.models import WorkLinePosition

    for model in (WorkLine, WorkLinePosition, Device, DeviceRoute, TransportTask):
        model.__table__.to_metadata(metadata)
    engine = flow.factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.insert().values(
                id=1,
                line_code="LINE-1",
                line_name="roller",
                business_code="first",
                position_bindings={"FIVE_RACK": "FIVE", "INLET": "IN", "OUTLET": "OUT"},
                device_bindings=flow.devices,
            )
        )
        line = await session.get(WorkLine, 1)
        await configure_rack_positions(session, line)

    async def lock_line(session, line):
        await session.execute(select(WorkLine.id).where(WorkLine.id == line).with_for_update())

    flow.lock_scan_line = lock_line

    async def advance(kind, identity):
        async with flow.factory() as session, session.begin():
            if engine.dialect.name == "postgresql":
                await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            await lock_line(session, 1)
            return await flow.roller.advance(session, RollerWorkRef(kind, identity))

    async def demand(*, face="A", rack="R1", revision=1):
        async with flow.factory.begin() as session:
            await flow.roller.apply_supply_change(
                session,
                SupplyChange(
                    1, "task-original", str(uuid7()), (SourceFaceRef(revision, rack, face),), ()
                ),
            )
            rows = await flow.rows(session, "face_work")
            return next(
                r["id"]
                for r in rows
                if r["rack_id"] == rack
                and r["rack_face"] == face
                and r["plan_revision"] == revision
            )

    async def rows(key):
        async with flow.factory() as session:
            return await flow.rows(session, key)

    async def decide(message_id, data):
        async with flow.factory.begin() as session:
            row = await session.get(ReliableMessage, message_id)
            row.status, row.last_http_status, row.last_verdict = "DONE", 200, "DELIVERED"
            row.response_raw = json.dumps(
                dict(operation_id=row.operation_id, code="DECIDED", timestamp=2, data=data)
            ).encode()

    async def callback(operation, data):
        async with flow.factory.begin() as session:
            result = await receive(
                session,
                json.dumps(
                    dict(operation=operation, operation_id=str(uuid7()), timestamp=3, data=data)
                ).encode(),
            )
            assert result.status_code in (200, 202), result

    async def complete_transport(
        transport_id, *, revision=1, placed=True, status="SUCCEEDED", count=None
    ):
        async with flow.factory() as session:
            task = await session.scalar(
                select(TransportTask).where(TransportTask.transport_task_id == transport_id)
            )
            frozen = task.frozen
        if frozen["kind"] in ("RACK_MOVE", "RACK_ROTATE"):
            await callback(
                "transport.task.resulted@v1",
                dict(
                    transport_task_id=transport_id,
                    kind=frozen["kind"],
                    rack_id=frozen["rack_id"],
                    outcome_revision=revision,
                    status="SUCCEEDED",
                    final_position=frozen["target"],
                    arrival_face=frozen["target_face"],
                ),
            )
            return
        results = []
        for i, move in enumerate(frozen["moves"]):
            succeeded = status == "SUCCEEDED" and (count is None or i < count)
            if succeeded and placed and move["target"]["kind"] == "RACK_BIN_SLOT":
                await callback(
                    "transport.task.member_position_changed@v1",
                    dict(
                        transport_task_id=transport_id,
                        container_id=move["container_id"],
                        milestone="TARGET_PLACED",
                        final_position=move["target"],
                    ),
                )
            result = dict(
                container_id=move["container_id"],
                status="SUCCEEDED" if succeeded else "FAILED",
                final_position=move["target"] if succeeded else move["source"],
            )
            if status == "UNKNOWN":
                result.update(
                    final_position=None, position_unknown=True, failure_code="POSITION_UNKNOWN"
                )
            elif not succeeded:
                result["failure_code"] = "RCS_EXECUTION_FAILED"
            results.append(result)
        await callback(
            "transport.task.resulted@v1",
            dict(
                transport_task_id=transport_id,
                kind="BIN_MOVE",
                outcome_revision=revision,
                results=results,
            ),
        )

    async def ready_supply(*, count=5):
        face_id = await demand()
        await advance("FACE", face_id)
        async with flow.factory() as session:
            face = await flow.roller.original_record(session, "face_work", face_id)
            job = await dispatch.progress(session, job_id=face["rack_job_id"])
        await complete_transport(job.actions[-1].transport_task_id)
        await advance("FACE", face_id)
        face = next(r for r in await rows("face_work") if r["id"] == face_id)
        if count is None:
            return face_id
        await decide(
            face["feed_message_id"],
            dict(
                result="READY",
                bins=[
                    dict(
                        bin_code=f"A{100 + i:09}",
                        source_locator=dict(
                            type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id=f"S{i}"
                        ),
                    )
                    for i in range(count)
                ],
            ),
        )
        await advance("FACE", face_id)
        return face_id

    async def bins(count=1, *, offset=0):
        for number in range(offset + 1, offset + count + 1):
            await flow.scan("SCAN3", f"A{number:09}-B", timestamp=number * 10)
            await flow.scan("SCAN4", f"A{number:09}-B", timestamp=number * 10 + 1)
            row = next(
                r
                for r in await rows("bin_return")
                if r["bin_code"] == f"A{number:09}" and r["is_current"]
            )
            await flow.result(row["scan4_command_code"], "SCAN4")
            await advance("RETURN", row["id"])

    flow.advance_current, flow.demand_current, flow.current_rows = advance, demand, rows
    flow.decide_current, flow.complete_transport = decide, complete_transport
    flow.ready_supply_current, flow.current_bins = ready_supply, bins
    try:
        yield flow
    finally:
        async with flow.factory.begin() as session:
            await session.execute(
                WorkLinePosition.__table__.delete().where(WorkLinePosition.work_line_id == 1)
            )
            await session.execute(WorkLine.__table__.delete().where(WorkLine.id == 1))
