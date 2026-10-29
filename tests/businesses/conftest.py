"""A concrete document → decision → move → report business, assembled only by tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.businesses.runtime import BusinessRuntime, Capabilities
from app.core.identifiers import uuid7
from app.http_client import AsyncHttpClient, HttpClientConfig
from app.integrations.ecs_command import (
    EcsCommands,
    RoleRoutes,
    compile_commands,
    ecs_command,
)
from app.integrations.ecs_command.clients import DynamicClients
from app.integrations.ecs_command.contracts import ErrorDetail
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.wms_callback import Accepted, Conflict, Duplicate, Rejected
from app.integrations.wms_request import Endpoint, RequestPayload, WmsRequests, request_operation
from app.main import app
from app.models import CallbackReceipt, Device, EcsCommand, EcsResultEvidence, WorkLine
from app.reliable.config import TargetConfig
from app.reliable.sender import Sender
from app.reliable.store import MessageStore
from app.services.device_bindings import resolve_binding
from app.services.device_routes import resolve_route
from app.services.workline_service import business_configuration_issue
from tests.helpers import (
    FlowService,
    business_flow_fixture,
    loopback_app,
    simulator_client_factory,
)


class Position(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    position: str


@ecs_command(
    "MOVE",
    device_roles=("SEAM_A",),
    annex_id="mock-cycle",
    annex_version="1",
    result=Position,
    error=ErrorDetail,
    timeout_min_ms=1,
    timeout_max_ms=1000,
)
class Move(Position):
    pass


class Decide(RequestPayload):
    execution_id: str
    position: str


class Record(RequestPayload):
    execution_id: str
    position: str


class MockCycleService(FlowService):
    """Own concrete phase references in next_intent and consumed results in evidence."""

    async def accept(self, ctx, payload):
        if hasattr(ctx, "binding") and ctx.device_code != payload.device_code:
            ctx.note("DEVICE_MISMATCH")
            return Rejected("DEVICE_MISMATCH")
        original = (
            (
                await ctx.session.execute(
                    select(self.table).where(self.table.c.id == payload.execution_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if original:
            if original["accepted_payload"] != payload.model_dump(mode="json"):
                ctx.note("EXECUTION_CONFLICT")
                return Conflict()
            return Duplicate()
        result = await super().accept(ctx, payload)
        if isinstance(result, Duplicate):
            # A concurrent receiver may commit while base accept waits for the binding locks.
            original_payload = await ctx.session.scalar(
                select(self.table.c.accepted_payload).where(self.table.c.id == payload.execution_id)
            )
            if original_payload != payload.model_dump(mode="json"):
                ctx.note("EXECUTION_CONFLICT")
                return Conflict()
        if isinstance(result, Accepted):
            binding = await resolve_binding(ctx.session, payload.device_code)
            await ctx.session.execute(
                self.table.update()
                .where(self.table.c.id == payload.execution_id)
                .values(
                    next_intent={
                        "stage": "accepted",
                        "position": binding.position_bindings["INLET"],
                    }
                )
            )
        return result

    async def advance(self, execution_id, *, attempts=3, hook=None):
        table = self.table
        for attempt in range(attempts):
            try:
                async with self.factory() as session, session.begin():
                    await session.connection(
                        execution_options={"isolation_level": "REPEATABLE READ"}
                    )
                    if hook:
                        await hook("opened", attempt, session, None)
                    located = (
                        (await session.execute(select(table).where(table.c.id == execution_id)))
                        .mappings()
                        .one()
                    )
                    if hook:
                        await hook("located", attempt, session, None)
                    line = await session.scalar(
                        select(WorkLine).where(WorkLine.id == located["line_id"]).with_for_update()
                    )
                    device = await session.scalar(
                        select(Device).where(Device.id == located["device_id"]).with_for_update()
                    )
                    row = (
                        (
                            await session.execute(
                                select(table).where(table.c.id == execution_id).with_for_update()
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if row["business"] != self.code:
                        raise ValueError("execution belongs to another business")
                    if row["done"]:
                        return "done"
                    if (
                        line is None
                        or device is None
                        or device.device_code != row["device_code"]
                        or device.work_line_id != line.id
                        or line.device_bindings.get(row["device_role"]) != device.device_code
                    ):
                        return "ORIGINAL_BINDING_CHANGED"
                    binding = await resolve_binding(session, device.device_code)
                    issue = await business_configuration_issue(
                        session, binding, self.code, definition_lookup=lambda _: self.definition
                    )
                    if issue:
                        return issue
                    intent = dict(row["next_intent"])
                    evidence = dict(row["evidence"] or {})
                    if intent.get("reason"):
                        return "blocked"
                    stage, reason = intent["stage"], None
                    if stage == "accepted":
                        operation_id = str(uuid7())
                        ref = await self.capabilities.wms.send(
                            session,
                            Decide(execution_id=execution_id, position=intent["position"]),
                            operation_id=operation_id,
                        )
                        intent["decision"] = dict(
                            message_id=ref.message_id,
                            operation="mock_cycle.decide@v1",
                            operation_id=operation_id,
                        )
                        intent["stage"] = "decision"
                    elif stage in {"decision", "report"}:
                        result = await self.capabilities.wms.get_result(session, **intent[stage])
                        if result.availability == "FOUND" and not result.delivery_terminal:
                            if result.delivery_uncertain:
                                reason = f"{stage.upper()}_UNKNOWN"
                            else:
                                return "waiting"
                        else:
                            expected = "DECIDED" if stage == "decision" else "RECORDED"
                            valid = (
                                result.availability == "FOUND"
                                and result.response_state == "VALID"
                                and result.response_code == expected
                            )
                            data = result.response_data or {}
                            valid &= (
                                data.get("allow") is True if stage == "decision" else data == {}
                            )
                            if not valid:
                                reason = f"{stage.upper()}_INVALID_OR_REJECTED"
                            else:
                                evidence[stage] = asdict(result)
                                if stage == "decision":
                                    command = await self.capabilities.ecs.create(
                                        session,
                                        owner=self.owner,
                                        call_key=execution_id,
                                        device_code=device.device_code,
                                        command=Move(position=intent["position"]),
                                        timeout_ms=100,
                                    )
                                    intent["command"] = dict(
                                        command_code=command.command_code, call_key=execution_id
                                    )
                                    intent["stage"] = "command"
                                else:
                                    intent["stage"] = "done"
                    else:
                        ref = intent["command"]
                        result = await self.capabilities.ecs.get_command(
                            session, owner=self.owner, command_code=ref["command_code"]
                        )
                        if result is None or result.needs_reconciliation:
                            reason = "COMMAND_INVALID"
                        elif result.status in {"RECONCILING", "REJECTED", "EXPIRED", "FAILED"}:
                            reason = result.hold_reason or result.status
                        elif result.status != "SUCCEEDED" or result.result is None:
                            return "waiting"
                        elif result.result["data"]["position"] != intent["position"]:
                            reason = "WRONG_POSITION"
                        else:
                            evidence["command"] = asdict(result)
                            operation_id = str(uuid7())
                            ref = await self.capabilities.wms.send(
                                session,
                                Record(execution_id=execution_id, position=intent["position"]),
                                operation_id=operation_id,
                            )
                            intent["report"] = dict(
                                message_id=ref.message_id,
                                operation="mock_cycle.record@v1",
                                operation_id=operation_id,
                            )
                            intent["stage"] = "report"
                    if reason:
                        intent["reason"] = reason
                        intent["failure"] = asdict(result) if result is not None else None
                    if hook:
                        await hook("read", attempt, session, evidence)
                    done = intent["stage"] == "done"
                    await session.execute(
                        table.update()
                        .where(table.c.id == execution_id)
                        .values(next_intent=intent, evidence=evidence, pending=not done, done=done)
                    )
                    if hook:
                        await hook("written", attempt, session, evidence)
                if hook:
                    await hook("committed", attempt, session, evidence)
                return "blocked" if reason else intent["stage"]
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "40001":
                    raise
        return "retry_later"


@asynccontextmanager
async def mock_cycle_fixture(factory, monkeypatch):
    async with business_flow_fixture(factory, monkeypatch) as flow:
        request_operation("mock_cycle.decide@v1", Endpoint.DECISIONS)(Decide)
        request_operation("mock_cycle.record@v1", Endpoint.FACTS)(Record)
        bindings, annexes = compile_commands(Move)
        routes = RoleRoutes(
            bindings, annexes=annexes, resolve_binding=resolve_binding, resolve_route=resolve_route
        )
        commands = EcsCommands(flow.outbox, "seam-ecs", routes, annexes)
        lifecycle = EcsLifecycle(commands)
        flow.models = SimpleNamespace(decide=Decide, move=Move, record=Record)
        flow.commands = commands
        flow.outbox.lifecycles = {"ecs-command": lifecycle}
        monkeypatch.setattr(app.state, "ecs_commands", commands, raising=False)
        flow.peer = SimpleNamespace(
            decision="allow",
            command="ack",
            report="record",
            received=[],
            command_started=asyncio.Event(),
            release_ack=asyncio.Event(),
        )
        peer = FastAPI()

        @peer.post("/api/v1/wes/{endpoint}")
        async def wms(endpoint: str, request: Request):
            raw = await request.body()
            body = json.loads(raw)
            flow.peer.received.append((endpoint, raw))
            mode = flow.peer.decision if endpoint == "decisions" else flow.peer.report
            if mode == "invalid":
                return {"invalid": True}
            if mode == "reject":
                return JSONResponse(
                    dict(
                        operation_id=body["operation_id"],
                        code="REJECTED",
                        timestamp=1,
                        data={"reason_code": "INVALID_DATA"},
                    ),
                    status_code=422,
                )
            data = (
                {"allow": mode == "allow"}
                if endpoint == "decisions"
                else ({"execution_id": body["data"]["execution_id"]} if mode == "nonempty" else {})
            )
            return dict(
                operation_id=str(uuid7()) if mode == "wrong_identity" else body["operation_id"],
                code="DECIDED" if endpoint == "decisions" else "RECORDED",
                timestamp=1,
                data=data,
            )

        @peer.post("/api/v1/device/command")
        async def ecs(request: Request):
            flow.peer.received.append(("command", await request.body()))
            if flow.peer.command == "hold_ack":
                flow.peer.command_started.set()
                await flow.peer.release_ack.wait()
            if flow.peer.command == "invalid":
                return {"invalid": True}
            if flow.peer.command == "reject":
                return JSONResponse({"code": 404, "message": "DEVICE_NOT_FOUND"}, status_code=404)
            return {"code": 200, "message": "ACK"}

        async with loopback_app(peer) as origin:
            flow.targets["seam-wms"] = replace(flow.targets["seam-wms"], base_url=origin)
            flow.targets["cycle-facts"] = TargetConfig("cycle-facts", origin, "wms-facts")
            flow.origins[:] = [origin] * 3

            def rebuild():
                old = flow.runtime
                service = MockCycleService(
                    "seam_a",
                    old.definitions["seam_a"],
                    Capabilities(
                        commands,
                        WmsRequests(flow.outbox),
                        old.services["seam_a"].capabilities.transport,
                        factory,
                    ),
                    factory,
                    flow.table,
                )
                service.owner = flow.owner
                return BusinessRuntime(old.definitions, {**old.services, "seam_a": service})

            flow.rebuild = rebuild
            flow.runtime = rebuild()
            monkeypatch.setattr(app.state, "business_runtime", flow.runtime)
            store = MessageStore(factory, lifecycles={"ecs-command": lifecycle})
            sender = Sender(
                store,
                flow.targets,
                flow.outbox.adapters,
                {
                    name: AsyncHttpClient(HttpClientConfig(origin, name))
                    for name in ("seam-wms", "cycle-facts")
                },
                provider=DynamicClients(factory=simulator_client_factory([origin])),
            )

            async def dispatch():
                count = await sender.tick(maintain=False)
                if sender.inflight:
                    await asyncio.gather(*tuple(sender.inflight))
                return count

            async def result(execution_id, *, position=None, failed=False, device_code=None):
                row = next(r for r in await flow.rows() if r["id"] == execution_id)
                body = dict(
                    command_code=row["next_intent"]["command"]["command_code"],
                    device_code=device_code or row["device_code"],
                    result="FAILED" if failed else "SUCCESS",
                    finish_time=1,
                    data={"position": position or row["next_intent"]["position"]},
                    error_detail={"code": "MOVE_FAILED", "msg": "mock failure"} if failed else None,
                )
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    return await client.post("/api/v1/callback/result", json=body)

            flow.dispatch, flow.result = dispatch, result
            flow.sender, flow.store = sender, store
            try:
                yield flow
            finally:
                flow.peer.release_ack.set()
                await sender.aclose()
                async with factory.begin() as session:
                    codes = select(EcsCommand.command_code).where(
                        EcsCommand.owner_domain == flow.owner.domain
                    )
                    digests = select(EcsResultEvidence.digest).where(
                        EcsResultEvidence.command_code.in_(codes)
                    )
                    await session.execute(
                        delete(CallbackReceipt).where(
                            CallbackReceipt.operation == "ecs.result",
                            CallbackReceipt.operation_id.in_(digests),
                        )
                    )
                    await session.execute(
                        delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
                    )


@pytest.fixture
async def mock_cycle(pg_sessions, monkeypatch):
    async with mock_cycle_fixture(pg_sessions, monkeypatch) as flow:
        await flow.configure()
        yield flow


@pytest.fixture
async def coarse_scan(callback_store, monkeypatch):
    """Own a temporary scan table and two bound lines for the deployed scan entry."""
    import sys

    from app.businesses import catalog
    from app.businesses.coarse_sorting.service import CoarseSortingService
    from app.businesses.runtime import build_runtime
    from app.integrations import discovery
    from app.integrations.ecs_callback import registry as events
    from app.models import DeviceRoute, ReliableMessage
    from app.reliable import Outbox
    from tests.helpers import (
        add_bound_device,
        coarse_sorting_composition,
        coarse_sorting_scan_table,
    )

    table = coarse_sorting_scan_table("coarse_scan_test_" + uuid7().hex)
    composition = coarse_sorting_composition(table)
    definition = composition.DEFINITION
    engine = callback_store.kw["bind"]
    codes, line_ids, route_ids, conveyor_codes = [], [], [], []
    async with engine.begin() as connection:
        await connection.run_sync(table.create)
    monkeypatch.setattr(events, "_registry", {})
    monkeypatch.setattr(app.state, "ecs_event_binding_resolver", resolve_binding)
    state = SimpleNamespace(
        factory=callback_store,
        table=table,
        definition=definition,
        codes=codes,
        line_ids=line_ids,
        route_ids=route_ids,
        conveyor_codes=conveyor_codes,
    )

    def event(*, device=0, timestamp=1793206799000, changes=None, omit=()):
        data = {
            "barcode": " P032-0561,原始条码 ",
            "reel_diameter": 15,
            "reel_thickness": 20,
        }
        data.update(changes or {})
        for key in omit:
            data.pop(key)
        return {
            "device_code": codes[device],
            "event_type": "SCAN_COMPLETED",
            "timestamp": timestamp,
            "data": data,
        }

    async def post(body):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/v1/callback/event", json=body)

    async def rows():
        async with callback_store() as session:
            return list((await session.execute(select(table).order_by(table.c.id))).mappings())

    async def outbound_counts():
        from sqlalchemy import func

        async with callback_store() as session:
            return (
                await session.scalar(select(func.count()).select_from(ReliableMessage)),
                await session.scalar(select(func.count()).select_from(EcsCommand)),
            )

    state.event, state.post, state.rows, state.outbound_counts = event, post, rows, outbound_counts

    original_accept = CoarseSortingService.accept_scan

    async def observe_acceptance(service, ctx, payload):
        with monkeypatch.context() as guard:

            def forbidden(*args, **kwargs):
                raise AssertionError("business acceptance cannot own a transaction")

            for method in ("begin", "begin_nested", "commit", "rollback", "connection"):
                guard.setattr(ctx.session, method, forbidden)
            await original_accept(service, ctx, payload)

        if getattr(state, "after_accept", None) is not None:
            await state.after_accept(ctx)

    monkeypatch.setattr(CoarseSortingService, "accept_scan", observe_acceptance)
    entry_module = "app.businesses.coarse_sorting.ecs_event.scan_completed"
    monkeypatch.setattr(catalog, "entries", lambda: (composition,))
    monkeypatch.setattr(catalog, "_validated_entries", None)
    monkeypatch.setattr(catalog, "_registration_guard_enabled", catalog._registration_guard_enabled)

    def install():
        monkeypatch.setattr(events, "_registry", {})
        monkeypatch.setattr(discovery, "_loaded_formal", set())
        monkeypatch.delitem(sys.modules, entry_module, raising=False)
        discovery.load_formal_modules("ecs_event")
        bindings, annexes = catalog.validate_business_contracts()
        from app.services.device_routes import DeviceRoutes

        outbox = Outbox({}, {})
        commands = EcsCommands(
            outbox, "coarse-test", DeviceRoutes(bindings, annexes=annexes), annexes
        )
        state.runtime = build_runtime(
            outbox=outbox, ecs_commands=commands, session_factory=callback_store
        )
        monkeypatch.setattr(app.state, "business_runtime", state.runtime, raising=False)

    state.install = install
    try:
        install()
        async with callback_store.begin() as session:
            for _ in range(2):
                code = "COARSE_TEST_" + uuid7().hex
                line, device, route = await add_bound_device(
                    session, code, "http://127.0.0.1:1", role="INPUT_ARM"
                )
                conveyor_code = "CONVEYOR_" + uuid7().hex
                conveyor_route = DeviceRoute(
                    id=str(uuid7()), device_code=conveyor_code, ecs_origin="http://127.0.0.1:1"
                )
                session.add(conveyor_route)
                await session.flush()
                session.add(
                    Device(
                        device_code=conveyor_code,
                        display_name=conveyor_code,
                        current_route_id=conveyor_route.id,
                        work_line_id=line.id,
                    )
                )
                line.device_bindings = {"INPUT_ARM": code, "CONVEYOR": conveyor_code}
                conveyor_codes.append(conveyor_code)
                route_ids.append(conveyor_route.id)
                line.business_code = "coarse_sorting"
                line.position_bindings = {}
                codes.append(code)
                line_ids.append(line.id)
                route_ids.append(route.id)
        yield state
    finally:
        async with callback_store.begin() as session:
            await session.execute(
                delete(Device).where(Device.device_code.in_([*codes, *conveyor_codes]))
            )
            await session.execute(delete(WorkLine).where(WorkLine.id.in_(line_ids)))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id.in_(route_ids)))
        async with engine.begin() as connection:
            await connection.run_sync(table.drop)


@pytest.fixture
async def coarse_forward(coarse_scan, monkeypatch):
    """Real formal service and Sender against an explicit loopback WMS/ECS peer."""
    from app.businesses import catalog
    from app.businesses.runtime import build_runtime
    from app.integrations.ecs_command.protocol import EcsCommandAdapter
    from app.integrations.wms_request import ADAPTERS
    from app.models import DeviceRoute, ReliableMessage
    from app.reliable import Outbox
    from app.services.device_routes import DeviceRoutes

    flow = coarse_scan
    flow.peer = SimpleNamespace(
        decision={"result": "OK", "binding_id": "BIND-ORIGINAL"},
        decision_code="DECIDED",
        decision_status=200,
        command="ack",
        requests=[],
        commands=[],
        started=asyncio.Event(),
        release=asyncio.Event(),
    )
    peer = FastAPI()

    @peer.post("/api/v1/wes/decisions")
    async def decide(request: Request):
        raw = await request.body()
        body = json.loads(raw)
        flow.peer.requests.append(raw)
        return JSONResponse(
            dict(
                operation_id=body["operation_id"],
                code=flow.peer.decision_code,
                timestamp=1,
                data=flow.peer.decision,
            ),
            status_code=flow.peer.decision_status,
        )

    @peer.post("/api/v1/device/command")
    async def command(request: Request):
        flow.peer.commands.append(await request.body())
        flow.peer.started.set()
        if flow.peer.command == "hold_ack":
            await flow.peer.release.wait()
        if flow.peer.command == "invalid":
            return {"invalid": True}
        return {"code": 200, "message": "ACK"}

    async with loopback_app(peer) as origin:
        async with flow.factory.begin() as session:
            routes = await session.scalars(
                select(DeviceRoute).where(DeviceRoute.id.in_(flow.route_ids))
            )
            for route in routes:
                route.ecs_origin = origin
        bindings, annexes = catalog.validate_business_contracts()
        targets = {
            "coarse-wms": TargetConfig("coarse-wms", origin, "wms-decisions"),
            "ecs-local": TargetConfig("ecs-local", None, "ecs-command", address_mode="SNAPSHOT"),
        }
        adapters = {**ADAPTERS, "ecs-command": EcsCommandAdapter()}
        commands = EcsCommands(None, "ecs-local", DeviceRoutes(bindings, annexes=annexes), annexes)
        lifecycle = EcsLifecycle(commands)
        outbox = Outbox(targets, adapters, lifecycles={"ecs-command": lifecycle})
        commands.outbox = outbox
        flow.commands, flow.outbox = commands, outbox

        def rebuild():
            flow.runtime = build_runtime(
                outbox=outbox, ecs_commands=commands, session_factory=flow.factory
            )
            flow.service = flow.runtime.services["coarse_sorting"]
            monkeypatch.setattr(app.state, "business_runtime", flow.runtime)
            monkeypatch.setattr(app.state, "ecs_commands", commands, raising=False)

        rebuild()
        flow.rebuild = rebuild
        store = MessageStore(flow.factory, lifecycles={"ecs-command": lifecycle})
        sender = Sender(
            store,
            targets,
            adapters,
            {"coarse-wms": AsyncHttpClient(HttpClientConfig(origin, "coarse-wms"))},
            provider=DynamicClients(factory=simulator_client_factory([origin])),
        )
        flow.sender, flow.store = sender, store

        async def dispatch():
            count = await sender.tick(maintain=False)
            if sender.inflight:
                await asyncio.gather(*tuple(sender.inflight))
            return count

        async def result(row, **changes):
            body = dict(
                command_code=row["command_code"],
                device_code=row["conveyor_device_code"],
                result="SUCCESS",
                finish_time=1,
                data={"actual_qty": 1, "location": "STATION_PIPELINE1_OUTPUT1"},
                error_detail=None,
            )
            body.update(changes)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post("/api/v1/callback/result", json=body)

        async def prepare_command():
            assert (await flow.post(flow.event())).status_code == 200
            row = (await flow.rows())[0]
            assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
            assert await dispatch() == 1
            assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
            return (await flow.rows())[0]

        flow.dispatch, flow.result, flow.prepare_command = dispatch, result, prepare_command
        try:
            yield flow
        finally:
            flow.peer.release.set()
            await sender.aclose()
            async with flow.factory.begin() as session:
                codes = select(EcsCommand.command_code).where(
                    EcsCommand.device_code.in_(flow.conveyor_codes)
                )
                await session.execute(
                    delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
                )
                await session.execute(
                    delete(EcsCommand).where(EcsCommand.device_code.in_(flow.conveyor_codes))
                )
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.target.in_(targets))
                )
