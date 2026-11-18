"""Run the formal X-Ray entry points and Sender against disposable peers."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.businesses import catalog
from app.businesses.runtime import build_runtime
from app.businesses.xray.mock_wms import XrayWmsMock
from app.controllers.dev_mock_wms import decide_request
from app.core import get_settings
from app.core.identifiers import uuid7
from app.http_client import AsyncHttpClient
from app.integrations import history
from app.integrations.ecs_command import EcsCommands
from app.integrations.ecs_command.clients import DynamicClients
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import EcsCommandAdapter
from app.integrations.wms_request import ADAPTERS, WmsRequests
from app.integrations.wms_transport import ADAPTERS as TRANSPORT_ADAPTERS
from app.main import app
from app.models import Device, DeviceRoute, WorkLine, WorkLinePosition
from app.reliable import Outbox
from app.reliable.config import TargetConfig
from app.reliable.sender import Sender
from app.reliable.store import MessageStore
from app.reliable.testing import FakeClock
from app.services.device_routes import DeviceRoutes, authorize_new_command
from tests.helpers import (
    add_bound_device,
    deliver_transport_callback,
    loopback_app,
    simulator_client_factory,
)

TEMPLATE = "XX123456789"
MOCK_BARCODES = (
    "PTEST-PART-01,Q3000,MTEST-MPN-01,D260101,LTEST-LOT-01,STEST-SERIAL-01",
    "PTEST-PART-02,Q50000,MTEST-MPN-02,D260102,LTEST-LOT-02,STEST-SERIAL-02",
)
LABEL = {
    "ID": TEMPLATE,
    "PHHPN": "part",
    "SPKG": "planned-new",
    "MMfrPN": "mpn",
    "VVendor": "vendor",
    "QQTYPC": "0",
    "Location": "",
    "DDC": "2623",
    "DateTime": "2026-10-29 14:43:50",
    "LLC": "lot",
}


def test_xray_registers_in_formal_catalog():
    bindings, annexes = catalog.validate_business_contracts()
    assert "xray" in {item.code for item in catalog.definitions()}
    xray_bindings = [binding for binding in bindings if binding.annex_id.startswith("xray.")]
    assert {binding.task_type: binding.annex_version for binding in xray_bindings} == {
        "APPLY_PKG_DECISION": "3",
        "APPLY_COUNT_DECISION": "3",
        "APPLY_VERIFY_DECISION": "3",
        "PUT_TO_BIN": "3",
        "PUT_TO_RACK": "3",
    }
    assert all((binding.annex_id, "2") in annexes for binding in xray_bindings)
    old_bin = annexes[("xray.put_to_bin", "2")]
    old_bin.result_model.model_validate(
        {
            "reel_id": str(uuid4()),
            "actual_qty": 1,
            "position": "TARGET",
            "actual_target": {
                "location_id": "STATION_B",
                "location_type": "ONE_LAYER_RACK",
                "rack_id": "RACK-1",
                "bin_id": "BIN-D",
                "bin_type": "1",
                "bin_location": "D",
                "bin_cell_location": 8,
            },
        },
        context={"result": "SUCCESS"},
    )


@pytest.fixture
async def xray(callback_store, monkeypatch, request):
    factory = callback_store
    rack_template = getattr(request, "param", "F01")
    decisions, requests, commands, facts = {}, [], [], []
    mock_facts = []
    transports = []
    replies = {}
    command_hooks = {}
    peer = FastAPI()
    mock_peer = FastAPI()

    @mock_peer.post("/api/v1/wes/decisions")
    @peer.post("/api/v1/wes/decisions")
    async def decide(req: Request):
        body = await req.json()
        requests.append(body)
        if req.app is mock_peer:
            async with factory() as session:
                return await decide_request(req, session)
        op = body["operation"].split(".")[-1].split("@")[0]
        data = decisions[op]
        return {
            "operation_id": body["operation_id"],
            "code": "DECIDED",
            "timestamp": 1,
            "data": data(body) if callable(data) else data,
        }

    @mock_peer.post("/api/v1/wes/facts")
    @peer.post("/api/v1/wes/facts")
    async def report(req: Request):
        body = await req.json()
        facts.append(body)
        if req.app is mock_peer:
            mock_facts.append(body)
        status, code = replies.get("put_report", (200, "RECORDED"))
        return JSONResponse(
            {"operation_id": body["operation_id"], "code": code, "timestamp": 2, "data": {}},
            status_code=status,
        )

    @peer.post("/api/v1/wes/transport-requests")
    async def transport(req: Request):
        body = await req.json()
        transports.append(body)
        data = body["data"]
        if data["rcs_template_id"] == "CTU03" and replies.get("ctu03_storage"):
            detail = f"货架({data['rack_id']})当前已在库区，不能执行搬运任务(CTU03)"
            return JSONResponse(
                {
                    "operation_id": body["operation_id"],
                    "code": "REJECTED",
                    "timestamp": 1,
                    "data": {
                        "transport_task_id": data["transport_task_id"],
                        "reason_code": f"{detail}任务下发失败：{detail}",
                    },
                },
                status_code=422,
            )
        return JSONResponse(
            {
                "operation_id": body["operation_id"],
                "code": "RECEIVED",
                "timestamp": 1,
                "data": {"transport_task_id": body["data"]["transport_task_id"]},
            },
            status_code=202,
        )

    @peer.post("/api/v1/device/command")
    async def command(req: Request):
        body = await req.json()
        commands.append(body)
        if body["task_type"] in command_hooks:
            await command_hooks[body["task_type"]](body)
        status, message = replies.get(body["params"].get("decision"), (200, "ACK"))
        return JSONResponse({"code": status, "message": message}, status_code=status)

    async with loopback_app(peer) as origin, loopback_app(mock_peer) as mock_origin:
        device, arm = f"XRAY-{uuid4().hex}", f"ARM-{uuid4().hex}"
        point = f"RACK-{uuid4().hex}"
        async with factory.begin() as session:
            line, _, route = await add_bound_device(session, device, origin, role="XRAY")
            arm_route = DeviceRoute(id=str(uuid7()), device_code=arm, ecs_origin=origin)
            session.add(arm_route)
            await session.flush()
            session.add(
                Device(
                    device_code=arm,
                    display_name=arm,
                    current_route_id=arm_route.id,
                    work_line_id=line.id,
                )
            )
            line.business_code = "xray"
            line.device_bindings = {"XRAY": device, "XRAY_OUTPUT_ARM": arm}
            line.position_bindings = {"RETURN_RACK": point}
            session.add(WorkLinePosition(work_line_id=line.id, position_code=point))
        monkeypatch.setattr(
            get_settings(),
            "XRAY_RETURN_RACKS",
            {
                line.line_code: {
                    "location_code": point,
                    "location_id": "ECS-RACK-POINT",
                    "templates": {
                        "ENTER": "CTU01" if rack_template == "CTU01" else "F01",
                        "ROTATE": "CTU02" if rack_template == "CTU02" else "CK04",
                        "DEPART": "CTU03" if rack_template == "CTU03" else "F01",
                    },
                    "slots": {
                        "A": {
                            "A-03": {"rack_layer": 2, "rack_column": 3},
                            "A-04": {"rack_layer": 2, "rack_column": 4},
                        },
                        "B": {"B-03": {"rack_layer": 2, "rack_column": 3}},
                    },
                }
            },
        )
        monkeypatch.setattr(
            get_settings(),
            "XRAY_TRANSIT_TARGETS",
            {
                line.line_code: {
                    slot: {
                        "location_id": "STATION_B",
                        "location_type": "ONE_LAYER_RACK",
                        "rack_id": "RACK-1",
                        "bin_id": f"BIN-{slot}",
                        "bin_type": "1",
                        "bin_location": slot,
                        "bin_cell_location": 8,
                    }
                    for slot in "ABCD"
                }
            },
        )
        bindings, annexes = catalog.validate_business_contracts()
        targets = {
            "xray-decisions": TargetConfig("xray-decisions", origin, "wms-decisions"),
            "xray-facts": TargetConfig("xray-facts", origin, "wms-facts"),
            "xray-transport": TargetConfig("xray-transport", origin, "wms-transport"),
            "xray-ecs": TargetConfig("xray-ecs", None, "ecs-command", address_mode="SNAPSHOT"),
        }
        for protocol in ("wms-decisions", "wms-facts"):
            name = f"xray-mock-{protocol}"
            targets[name] = TargetConfig(name, mock_origin, protocol, explicit_only=True)
        adapters = {**ADAPTERS, **TRANSPORT_ADAPTERS, "ecs-command": EcsCommandAdapter()}
        clock = FakeClock()
        ecs = EcsCommands(
            None,
            "xray-ecs",
            DeviceRoutes(bindings, annexes=annexes),
            annexes,
            clock=clock,
            authorize_new=authorize_new_command,
        )

        def authorize_dispatch(command):
            return app.state.business_runtime.ecs_dispatch_authorizer(command)

        def authorize_transport(task):
            return app.state.business_runtime.transport_dispatch_authorizer(task)

        from app.integrations.wms_transport.dispatch import TransportDispatch

        lifecycle = {
            "ecs-command": EcsLifecycle(ecs, authorize_dispatch=authorize_dispatch),
            "wms-transport": TransportDispatch(authorize_transport),
        }
        outbox = Outbox(targets, adapters, lifecycles=lifecycle, records=history, clock=clock)
        ecs.outbox = outbox
        monkeypatch.setattr(app.state, "ecs_commands", ecs, raising=False)

        def rebuild():
            runtime = build_runtime(outbox=outbox, ecs_commands=ecs, session_factory=factory)
            service = runtime.services["xray"]
            service.clock = clock
            monkeypatch.setattr(app.state, "business_runtime", runtime, raising=False)
            mock_peer.state.business_runtime = runtime
            return service

        service = rebuild()

        def create_sender():
            store = MessageStore(factory, lifecycles=lifecycle, records=history, clock=clock)
            return Sender(
                store,
                targets,
                adapters,
                {
                    name: AsyncHttpClient(target.http_config())
                    for name, target in targets.items()
                    if target.address_mode == "STATIC"
                },
                provider=DynamicClients(factory=simulator_client_factory([origin, mock_origin])),
                clock=clock,
                monotonic=clock.monotonic,
            )

        sender = create_sender()
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        reel = str(uuid4())
        original = "001-original"
        stamp = 1793413814500

        event_reels = {}

        async def event(kind, *, event_id=None, reel_id=None, status=200, **data):
            event_id = event_id or str(uuid4())
            response = await client.post(
                "/api/v1/callback/event",
                json={
                    "device_code": device,
                    "event_type": kind,
                    "timestamp": stamp,
                    "data": {"event_id": event_id, "reel_id": reel_id or reel, **data},
                },
            )
            assert response.status_code == status, response.text
            event_reels[event_id] = reel_id or reel
            return event_id

        async def entry(**changes):
            return await event(
                "SCAN_COMPLETED",
                **{
                    "scan_stage": "ENTRY",
                    "scan_status": "OK",
                    "barcode": original,
                    "template_id": TEMPLATE,
                    **changes,
                },
            )

        async def result(cmd, *, status=200, failure=False, **data):
            params = cmd["params"]
            if "barcode" in params:
                data = {"barcode": params["barcode"], **data}
            body = {
                "command_code": cmd["command_code"],
                "device_code": cmd["device_code"],
                "result": "FAILED" if failure else "SUCCESS",
                "finish_time": stamp + 1,
                "data": {
                    "reel_id": params.get("reel_id") or event_reels[params["source_event_id"]],
                    **data,
                },
            }
            if failure:
                body["error_detail"] = {"code": "FAILED", "msg": "device failure"}
            response = await client.post("/api/v1/callback/result", json=body)
            assert response.status_code == status, response.text
            return body["finish_time"]

        async def dispatch():
            sent = await sender.tick()
            if sender.inflight:
                await asyncio.gather(*tuple(sender.inflight))
            return sent

        async def advance():
            return await service.scan_pending()

        def wakeups(*, page_size):
            from app import business_wakeups as notifications
            from app.tasks import business_tasks
            from app.tasks.celery_app import celery_app

            notices = []
            monkeypatch.setattr(notifications, "enqueue_business", notices.append)
            monkeypatch.setattr(
                app.state, "business_event_committed", notifications.event_committed, raising=False
            )
            monkeypatch.setattr(
                app.state,
                "business_committed",
                notifications.notify_business_committed,
                raising=False,
            )
            monkeypatch.setattr(
                app.state,
                "ecs_result_committed",
                notifications.ecs_result_committed,
                raising=False,
            )
            sender.store.on_result = notifications.wms_result_committed

            def runtime(_factory, _business):
                return SimpleNamespace(
                    progressions={"xray": lambda: service.scan_pending(limit=page_size)}
                )

            monkeypatch.setattr(business_tasks, "_runtime", runtime)
            beat = celery_app.conf.beat_schedule["advance-business-xray"]
            assert beat["task"] == "business.advance_pending"

            async def tick(business=None):
                return await business_tasks.advance_pending(factory, business or beat["args"][0])

            return notices, tick

        async def advance_reel(reel_id):
            from app.businesses.xray.models import Reel

            async with factory() as session:
                identity = await session.scalar(
                    select(Reel.id).where(Reel.work_line_id == line.id, Reel.reel_id == reel_id)
                )
            return await service.advance(identity)

        async def rack_jobs():
            async with factory() as session:
                return await service.racks.dispatch.jobs(
                    session, work_line_id=line.id, point=point, source_kinds=("XRAY_RETURN",)
                )

        async def change_binding():
            async with factory.begin() as session:
                bound = await session.get(WorkLine, line.id)
                bound.version += 1

        async def drain():
            for _ in range(8):
                await advance()
                if not await dispatch():
                    return
            pytest.fail("flow did not settle")

        def restart():
            nonlocal service
            service = rebuild()

        async def restart_sender():
            nonlocal sender
            await sender.aclose()
            sender = create_sender()

        async def dispatch_original_bin(source_event_id):
            from app.businesses.xray.ecs_command import LegacyPutToBin
            from app.businesses.xray.models import Reel, ReelOperation
            from app.businesses.xray.service import OWNER
            from app.integrations.ecs_command import compile_commands

            bindings, annexes = compile_commands(LegacyPutToBin)
            legacy = EcsCommands(
                outbox,
                "xray-ecs",
                DeviceRoutes(bindings, annexes=annexes),
                annexes,
                clock=clock,
            )
            params = LegacyPutToBin(
                reel_id=reel,
                source_event_id=source_event_id,
                target=get_settings().XRAY_TRANSIT_TARGETS[line.line_code]["D"],
            )
            async with factory.begin() as session:
                row = await session.scalar(
                    select(Reel).where(Reel.work_line_id == line.id, Reel.reel_id == reel)
                )
                operation = await session.scalar(
                    select(ReelOperation).where(
                        ReelOperation.reel_key == row.id, ReelOperation.stage == "OUTLET"
                    )
                )
                call_key = f"{row.id}:OUTLET"
                view = await legacy.create(
                    session,
                    owner=OWNER,
                    call_key=call_key,
                    device_code=arm,
                    timeout_ms=90_000,
                    command=params,
                )
                row.arm_device_code = arm
                row.actions = {
                    **row.actions,
                    "OUTLET": {
                        "command_code": view.command_code,
                        "message_id": view.message_id,
                        "call_key": call_key,
                        "device_code": arm,
                        "params": params.model_dump(mode="json"),
                        "operation_id": operation.operation_id,
                    },
                }
            with monkeypatch.context() as patch:
                patch.setattr(lifecycle["ecs-command"], "service", legacy)
                assert await dispatch() == 1
            return commands[-1]

        async def transport_result(body, **changes):
            data = body["data"]
            await deliver_transport_callback(
                factory,
                "transport.task.resulted@v1",
                {
                    "transport_task_id": data["transport_task_id"],
                    "kind": data["kind"],
                    "outcome_revision": 1,
                    "rack_id": data["rack_id"],
                    "status": "SUCCEEDED",
                    "final_position": data["target"],
                    **({"arrival_face": data["target_face"]} if "target_face" in data else {}),
                    **changes,
                },
            )

        def use_mock(*targets, fixed=False):
            nonlocal service
            mock_targets = {line.line_code: dict(zip(MOCK_BARCODES, targets, strict=True))}
            settings = get_settings()
            if fixed:
                point_settings = dict(settings.XRAY_RETURN_RACKS[line.line_code])
                point_settings.pop("templates")
                point_settings["slots"] = {"A": point_settings["slots"]["A"]}
                point_settings["fixed_test_rack"] = "RACK-A"
                monkeypatch.setattr(settings, "XRAY_RETURN_RACKS", {line.line_code: point_settings})
            monkeypatch.setattr(settings, "XRAY_WMS_MOCK_TARGETS", mock_targets)
            mock_peer.state.xray_wms_mock = XrayWmsMock(mock_targets, settings.XRAY_RETURN_RACKS)
            service = rebuild()

        @asynccontextmanager
        async def use_random_mock(thickness):
            nonlocal service
            settings = get_settings()
            mock_targets = {line.line_code: {"random": {"reel_thickness": thickness}}}

            def restart_peer():
                mock_peer.state.xray_wms_mock = XrayWmsMock(mock_targets, {})

            restart_peer()
            from app.businesses.xray.mock_storage import MockBase

            engine = factory.kw["bind"]
            async with engine.begin() as connection:
                await connection.run_sync(MockBase.metadata.create_all)
            try:
                monkeypatch.setattr(settings, "XRAY_WMS_MOCK_TARGETS", mock_targets)
                service = rebuild()
                yield restart_peer
            finally:
                async with engine.begin() as connection:
                    await connection.run_sync(MockBase.metadata.drop_all)

        try:
            yield SimpleNamespace(
                ecs_origin=origin,
                decisions=decisions,
                requests=requests,
                commands=commands,
                command_hooks=command_hooks,
                facts=facts,
                mock_facts=mock_facts,
                replies=replies,
                transports=transports,
                transport_result=transport_result,
                point=point,
                rack_template=rack_template,
                event=event,
                entry=entry,
                result=result,
                drain=drain,
                advance=advance,
                wakeups=wakeups,
                advance_reel=advance_reel,
                rack_jobs=rack_jobs,
                change_binding=change_binding,
                dispatch=dispatch,
                restart=restart,
                restart_sender=restart_sender,
                dispatch_original_bin=dispatch_original_bin,
                use_mock=use_mock,
                use_random_mock=use_random_mock,
                clock=clock,
                barcode=original,
                dialect=factory.kw["bind"].dialect.name,
            )
        finally:
            await client.aclose()
            await sender.aclose()
            from app.businesses._components.rack_dispatch.models import RackAction, RackJob
            from app.businesses.xray.models import RackPreparation, Reel, ReelEvent, ReelOperation

            async with factory.begin() as session:
                await session.execute(
                    delete(RackPreparation).where(RackPreparation.work_line_id == line.id)
                )
                for action in await session.scalars(
                    select(RackAction.id)
                    .where(RackAction.work_line_id == line.id)
                    .order_by(RackAction.ordinal.desc())
                ):
                    await session.execute(delete(RackAction).where(RackAction.id == action))
                await session.execute(delete(RackJob).where(RackJob.work_line_id == line.id))
                await session.execute(
                    delete(ReelOperation).where(
                        ReelOperation.reel_key.in_(
                            select(Reel.id).where(Reel.work_line_id == line.id)
                        )
                    )
                )
                await session.execute(delete(ReelEvent).where(ReelEvent.work_line_id == line.id))
                await session.execute(delete(Reel).where(Reel.work_line_id == line.id))
                await session.execute(delete(Device).where(Device.work_line_id == line.id))
                await session.execute(
                    delete(WorkLinePosition).where(WorkLinePosition.work_line_id == line.id)
                )
                await session.execute(delete(WorkLine).where(WorkLine.id == line.id))
                await session.execute(
                    delete(DeviceRoute).where(DeviceRoute.id.in_([route.id, arm_route.id]))
                )


@pytest.mark.parametrize("counted", [False, True])
async def test_normal_reel_and_transit_bin_resume_on_original_result(xray, counted):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1}
        if counted
        else {"result": "BYPASS_COUNT"},
        count_decide={"result": "OK", "label_variables": LABEL},
        verify_decide={"result": "OK"},
        put_target_decide={"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}},
    )
    entry_id = await x.entry()
    await x.drain()
    first = x.commands[0]
    assert first["params"]["source_event_id"] == entry_id
    assert first["params"]["barcode"] == x.barcode
    assert "reel_id" not in first["params"]
    if counted:
        assert first["params"]["reel_thickness"] == 8.1
        await x.event(
            "XRAY_COUNT_COMPLETED",
            barcode=x.barcode,
            template_id=TEMPLATE,
            count_status="OK",
            qty=0,
        )
        await x.drain()
        assert x.requests[1]["data"]["actual_qty"] == 0
        assert x.commands[1]["params"]["label"] == {"variables": LABEL}
        assert x.commands[1]["params"]["barcode"] == x.barcode
        assert "reel_id" not in x.commands[1]["params"]
        await x.result(first, decision="COUNT_REQUIRED", qty=0)
        await x.event(
            "SCAN_COMPLETED",
            scan_stage="VERIFY",
            scan_status="OK",
            barcode=x.barcode,
            old_barcode=x.barcode,
            new_barcode=LABEL["SPKG"],
        )
        await x.drain()
        assert x.requests[2]["data"]["expected_spkg"] == LABEL["SPKG"]
        await x.result(x.commands[1], decision="OK", label_status="APPLIED")
    mode = "COUNTED" if counted else "BYPASS"
    outlet = x.commands[-1]
    effective_barcode = LABEL["SPKG"] if counted else x.barcode
    arrived = dict(barcode=effective_barcode, mode=mode, ready_for_pick=True)
    if counted:
        arrived["new_barcode"] = LABEL["SPKG"]
        await x.event("MATERIAL_ARRIVED", **(arrived | {"barcode": x.barcode}), status=400)
        assert outlet["params"]["barcode"] == effective_barcode
        await x.result(outlet, decision="OK", mode=mode, barcode=effective_barcode)
    else:
        await x.result(outlet, decision="BYPASS_COUNT", mode=mode)
    await x.event("MATERIAL_ARRIVED", **arrived)
    await x.event("MATERIAL_ARRIVED", **arrived)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_BIN"
    assert put["params"]["barcode"] == effective_barcode
    assert "reel_id" not in put["params"]
    assert x.requests[-1]["data"]["barcode"] == effective_barcode
    target = put["params"]["target"]
    assert target == {
        "location_id": "STATION_B",
        "location_type": "ONE_LAYER_RACK",
        "bin_type": "1",
        "bin_location": target["bin_location"],
        "bin_cell_location": 8,
    }
    assert target["bin_location"] in "ABCD"
    assert len(x.requests) == (4 if counted else 2)
    assert len(x.commands) == (4 if counted else 2)
    assert not x.facts
    x.clock.advance(301)
    x.restart()
    await x.drain()
    assert len(x.commands) == (4 if counted else 2)
    assert x.commands[-1] == put
    assert not x.facts
    for invalid in ({"bin_type": 1}, {"bin_cell_location": "8"}, {"bin_location": "E"}):
        await x.result(
            put, status=400, actual_qty=1, position="TARGET", actual_target=target | invalid
        )
    x.replies["put_report"] = (503, "UNAVAILABLE")
    for _ in range(2):
        finished_at = await x.result(
            put,
            barcode=effective_barcode,
            actual_qty=1,
            position="TARGET",
            actual_target=target | {"rack_id": "RACK-1", "bin_id": "BIN-D", "reel_thickness": 20},
        )
        await x.drain()
    (report,) = x.facts
    assert report["data"] == {
        "workline_code": x.requests[-1]["data"]["workline_code"],
        "barcode": effective_barcode,
        "to_locator": {"type": "TRANSIT_BIN"},
        "occurred_at": finished_at,
    }
    assert report["operation_id"] != x.requests[-1]["operation_id"]
    x.replies["put_report"] = (200, "DUPLICATE")
    x.clock.advance(60)
    x.restart()
    await x.drain()
    assert x.facts == [report, report]
    assert len(x.requests) == (4 if counted else 2)
    assert len(x.commands) == (4 if counted else 2)
    assert not x.mock_facts
    assert all(
        not {"reel_id", "source_event_id"} & request["data"].keys()
        for request in (*x.requests, *x.facts)
    )


async def test_xray_trace_logs_committed_command_with_its_wms_operation(xray, caplog):
    import logging

    from app.xray_dispatch_trace import DispatchTrace, track_worker

    caplog.set_level(logging.INFO, logger="app.xray_dispatch_trace")
    xray.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    await xray.entry()
    task_id = str(uuid4())
    with track_worker(DispatchTrace(task_id, "beat")):
        await xray.drain()
        await xray.drain()
    stages = [record for record in caplog.records if getattr(record, "task_id", None) == task_id]
    assert [record.stage for record in stages] == [
        "worker_started",
        "ecs_command_registered",
        "worker_finished",
    ]
    assert f"wms_operation_id={xray.requests[0]['operation_id']}" in stages[1].getMessage()
    assert f"command_code={xray.commands[0]['command_code']}" in stages[1].getMessage()


async def test_wms_ok_cannot_release_a_reel_with_a_mismatched_ccd_label(xray):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1},
        count_decide={"result": "OK", "label_variables": LABEL},
        verify_decide={"result": "OK"},
    )
    await x.entry()
    await x.drain()
    await x.event(
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, template_id=TEMPLATE, count_status="OK", qty=0
    )
    await x.drain()
    for old_barcode in ({}, {"old_barcode": "ANOTHER-REEL"}):
        await x.event(
            "SCAN_COMPLETED",
            scan_stage="VERIFY",
            scan_status="OK",
            barcode=x.barcode,
            new_barcode=LABEL["SPKG"],
            status=400,
            **old_barcode,
        )
    await x.event(
        "SCAN_COMPLETED",
        scan_stage="VERIFY",
        scan_status="OK",
        barcode=x.barcode,
        old_barcode=x.barcode,
        new_barcode="other-reel-label",
    )
    await x.drain()
    assert x.requests[-1]["data"]["expected_spkg"] == LABEL["SPKG"]
    assert x.requests[-1]["data"]["new_barcode"] == "other-reel-label"
    assert [cmd["task_type"] for cmd in x.commands] == [
        "APPLY_PKG_DECISION",
        "APPLY_COUNT_DECISION",
    ]
    assert not x.facts


@pytest.mark.parametrize("wait", [False, True])
async def test_verify_ng_uses_entry_barcode_and_the_original_trigger(xray, wait):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1},
        count_decide={"result": "OK", "label_variables": LABEL},
        verify_decide={"result": "WAIT", "retry_after_ms": 1000}
        if wait
        else {"result": "NG", "reason_code": "REJECTED", "reason_message": "reject"},
    )
    await x.entry()
    await x.drain()
    await x.event(
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, template_id=TEMPLATE, count_status="OK", qty=0
    )
    await x.drain()
    verify_id = await x.event(
        "SCAN_COMPLETED",
        scan_stage="VERIFY",
        scan_status="OK",
        barcode=x.barcode,
        old_barcode=x.barcode,
        new_barcode="wrong-label",
    )
    await x.drain()
    trigger = verify_id
    if wait:
        trigger = await x.event(
            "XRAY_NG_REQUESTED",
            source_event_id=verify_id,
            reason_code="NO_CAPACITY",
            reason_message="cannot wait",
        )
        await x.drain()
    command = x.commands[-1]
    assert command["task_type"] == "APPLY_VERIFY_DECISION"
    assert command["params"] == {
        "barcode": x.barcode,
        "source_event_id": trigger,
        "decision": "NG",
        "reason": "cannot wait" if wait else "reject",
    }
    await x.result(command, decision="NG", discharged=True)
    await x.drain()
    assert [fact["data"]["barcode"] for fact in x.facts] == [x.barcode]


async def test_wait_ng_request_stops_normal_decision_and_reports_once(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    entry_id = await x.entry()
    await x.drain()
    assert not x.commands
    await x.event(
        "XRAY_NG_REQUESTED",
        source_event_id=entry_id,
        reason_code="NO_CAPACITY",
        reason_message="cannot wait",
    )
    x.clock.advance(2)
    await x.drain()
    assert len(x.requests) == 1
    (cmd,) = x.commands
    assert cmd["params"]["decision"] == "NG"
    await x.event("NG_DISCHARGED", stage="ENTRY", barcode=x.barcode, reason="discharged")
    await x.drain()
    await x.result(cmd, decision="NG", discharged=True)
    await x.drain()
    assert len(x.facts) == 1
    assert x.facts[0]["operation"] == "inbound.xray.ng_report@v1"
    assert "reel_id" not in x.facts[0]["data"]


@pytest.mark.parametrize("rejected", [False, True])
async def test_ng_request_waits_for_terminal_nonacceptance(xray, rejected):
    x = xray
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", "reel_thickness": 8.1}
    if rejected:
        x.replies["COUNT_REQUIRED"] = (422, "ANNEX_VALIDATION_FAILED")
    entry_id = await x.entry()
    await x.drain()
    assert len(x.commands) == 1
    request_id = await x.event(
        "XRAY_NG_REQUESTED",
        source_event_id=entry_id,
        reason_code="NO_CAPACITY",
        reason_message="cannot wait",
    )
    x.clock.advance(301)
    await x.drain()
    assert len(x.commands) == (2 if rejected else 1)
    if rejected:
        ng = x.commands[-1]
        assert ng["params"]["decision"] == "NG"
        assert ng["params"]["source_event_id"] == request_id
        await x.result(ng, decision="NG", discharged=True)
        await x.drain()
        assert x.facts[0]["data"]["reason_code"] == "NO_CAPACITY"
    else:
        assert not x.facts


async def test_wait_reasks_only_after_saved_retry_time(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    await x.entry()
    await x.drain()
    x.clock.advance(0.999)
    await x.drain()
    assert len(x.requests) == 1
    x.decisions["pkg_decide"] = {
        "result": "NG",
        "reason_code": "REJECTED",
        "reason_message": "reject this reel",
    }
    x.clock.advance(0.001)
    await x.drain()
    first, renewed = x.requests
    assert first["operation_id"] != renewed["operation_id"]
    assert first["data"] == renewed["data"]
    (cmd,) = x.commands
    assert cmd["params"]["decision"] == "NG"
    await x.result(cmd, decision="NG", discharged=True)
    await x.drain()
    assert x.facts[0]["data"]["reason_code"] == "REJECTED"


@pytest.mark.parametrize("result_first", [False, True])
async def test_count_conflict_blocks_label_after_saved_decision(xray, result_first):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1},
        count_decide={"result": "OK", "label_variables": LABEL},
    )
    await x.entry()
    await x.drain()
    (cmd,) = x.commands

    async def count():
        await x.event(
            "XRAY_COUNT_COMPLETED",
            barcode=x.barcode,
            template_id=TEMPLATE,
            count_status="OK",
            qty=1,
        )

    if result_first:
        await x.result(cmd, decision="COUNT_REQUIRED", qty=0)
        await count()
    else:
        await count()
        await x.advance()
        await x.dispatch()
        assert len(x.requests) == 2
        await x.result(cmd, decision="COUNT_REQUIRED", qty=0)
    await x.drain()
    assert len(x.requests) == (1 if result_first else 2)
    assert len(x.commands) == 1
    assert not x.facts


async def test_technical_discharge_can_precede_entry_context(xray):
    x = xray
    await x.event("NG_DISCHARGED", stage="ENTRY", barcode=None, reason="scanner failed")
    await x.drain()
    assert not x.facts
    await x.entry(
        barcode=None, scan_status="NG", reason_code="NO_BARCODE", reason_message="unreadable"
    )
    await x.drain()
    assert not x.requests
    assert not x.commands
    assert len(x.facts) == 1
    assert x.facts[0]["data"]["reason_code"] == "DEVICE_NG_DISCHARGED"


async def test_technical_count_discharge_does_not_wait_for_failed_result(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", "reel_thickness": 8.1}
    await x.entry()
    await x.drain()
    (cmd,) = x.commands
    await x.event(
        "XRAY_COUNT_COMPLETED",
        barcode=x.barcode,
        template_id=TEMPLATE,
        count_status="NG",
        reason_code="COUNT_FAILED",
        reason_message="measurement failed",
    )
    await x.event("NG_DISCHARGED", stage="COUNT", barcode=x.barcode, reason="count failed")
    await x.drain()
    assert len(x.facts) == 1
    await x.result(cmd, failure=True, status=400, decision="COUNT_REQUIRED", qty=10)
    await x.result(cmd, failure=True, failure_stage="COUNT", diagnostic_note="extension")
    await x.drain()
    assert len(x.facts) == len(x.commands) == len(x.requests) == 1


@pytest.mark.parametrize("wrong_field", ["bin_location", "barcode"])
async def test_wrong_bin_result_is_retained_without_put_report(xray, wrong_field):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "BYPASS_COUNT"},
        put_target_decide={"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}},
    )
    await x.entry()
    await x.drain()
    await x.event("MATERIAL_ARRIVED", barcode=x.barcode, mode="BYPASS", ready_for_pick=True)
    await x.drain()
    cmd = x.commands[-1]
    slot = cmd["params"]["target"]["bin_location"]
    target = {**cmd["params"]["target"], "bin_location": "B" if slot == "A" else "A"}
    data = dict(actual_qty=1, position="TARGET")
    if wrong_field == "bin_location":
        data["actual_target"] = target
    else:
        data["actual_target"] = cmd["params"]["target"]
        data["barcode"] = "ANOTHER-REEL"
    await x.result(cmd, **data)
    await x.drain()
    assert len(x.commands) == 2
    assert not x.facts


@pytest.mark.parametrize("put_task", ["PUT_TO_BIN", "PUT_TO_RACK"])
async def test_failed_put_accepts_actual_position_of_a_different_type(xray, put_task):
    x = xray
    if put_task == "PUT_TO_BIN":
        x.decisions["put_target_decide"] = {
            "result": "ACCEPT",
            "target_locator": {"type": "TRANSIT_BIN"},
        }
        await outlet(x)
        await x.drain()
        put = x.commands[-1]
        actual = {
            "location_id": "RACK-STATION",
            "location_type": "RETURN_RACK",
            "rack_id": "RACK-A",
            "rack_face": "A",
            "slot_id": "A-03",
            "rack_layer": 2,
            "rack_column": 3,
        }
    else:
        put = await ready_rack_put(x)
        actual = {
            "location_id": "STATION_B",
            "location_type": "ONE_LAYER_RACK",
            "bin_type": "1",
            "bin_location": "D",
            "bin_cell_location": 8,
        }
    assert put["task_type"] == put_task
    data = dict(position="OTHER", actual_target=actual, actual_qty=0)
    await x.result(put, failure=True, barcode=None, status=400, **data)
    await x.result(put, failure=True, **data)
    await x.drain()
    assert not x.facts


async def test_event_and_result_validate_known_fields_and_ignore_extensions(xray):
    x = xray
    await x.entry(status=400, barcode=None)
    await x.entry(status=400, event_id="not-a-uuid")
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", "reel_thickness": 8.1}
    await x.entry(extra=None)
    await x.drain()
    (cmd,) = x.commands
    await x.result(cmd, status=400, decision="COUNT_REQUIRED", qty=0, reel_id="not-a-uuid")
    await x.result(cmd, status=400, decision="COUNT_REQUIRED", qty=True)
    await x.result(cmd, status=400, failure=True, decision="COUNT_REQUIRED", qty=0, position="BAD")
    await x.result(cmd, decision="COUNT_REQUIRED", qty=0, extra=None)
    await x.drain()
    assert len(x.requests) == 1


@pytest.mark.parametrize("thickness", [{}, {"reel_thickness": 0}, {"reel_thickness": "8.1"}])
async def test_invalid_wms_count_thickness_retries_original_operation(xray, thickness):
    x = xray
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", **thickness}
    await x.entry()
    await x.drain()
    assert len(x.requests) == 1
    assert not x.commands
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", "reel_thickness": 8.1}
    x.clock.advance(60)
    await x.drain()
    assert len(x.requests) == 2
    assert x.requests[0] == x.requests[1]
    assert x.commands[0]["params"]["decision"] == "COUNT_REQUIRED"


async def test_event_cannot_override_terminal_nonacceptance(xray):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1},
        count_decide={"result": "OK", "label_variables": LABEL},
    )
    x.replies["COUNT_REQUIRED"] = (422, "ANNEX_VALIDATION_FAILED")
    await x.entry()
    await x.drain()
    await x.event(
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, template_id=TEMPLATE, count_status="OK", qty=0
    )
    await x.drain()
    assert len(x.requests) == len(x.commands) == 1


async def test_failed_reel_does_not_block_later_pending_reel(xray, monkeypatch):
    x = xray
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    first = await x.entry(reel_id=str(uuid4()), barcode="PENDING-FIRST")
    second = await x.entry(reel_id=str(uuid4()), barcode="PENDING-SECOND")
    send = WmsRequests.send

    async def fail_first(self, session, payload, **kwargs):
        reference = await send(self, session, payload, **kwargs)
        if payload.barcode == "PENDING-FIRST":
            raise history.EvidenceIntegrityError("original evidence unavailable")
        return reference

    with monkeypatch.context() as fault:
        fault.setattr(WmsRequests, "send", fail_first)
        await x.advance()
        await x.dispatch()
        assert [request["data"]["barcode"] for request in x.requests] == ["PENDING-SECOND"]

    await x.drain()
    assert [request["data"]["barcode"] for request in x.requests] == [
        "PENDING-SECOND",
        "PENDING-FIRST",
    ]
    assert [command["params"]["source_event_id"] for command in x.commands] == [second, first]


async def test_two_reels_share_one_unsettled_arm_command(xray):
    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    x.decisions.update(
        pkg_decide={"result": "BYPASS_COUNT"},
        put_target_decide={"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}},
    )
    reels_by_event = {await x.entry(reel_id=reel): reel for reel in (str(uuid4()), str(uuid4()))}
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.commands) == 2
    assert {c["params"]["source_event_id"] for c in x.commands} == reels_by_event.keys()
    for c in x.commands[:]:
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=reels_by_event[c["params"]["source_event_id"]],
            barcode=x.barcode,
            mode="BYPASS",
            ready_for_pick=True,
        )
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.commands) == 3
    put = x.commands[-1]
    await x.result(put, actual_qty=1, position="TARGET", actual_target=put["params"]["target"])
    await x.drain()
    assert len(x.facts) == 1
    assert len(x.commands) == 4
    assert x.commands[-1]["params"]["source_event_id"] != put["params"]["source_event_id"]


async def outlet(x, *, reel_id=None):
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    await x.entry(reel_id=reel_id)
    await x.drain()
    cmd = next(c for c in reversed(x.commands) if c["task_type"] == "APPLY_PKG_DECISION")
    await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
    return await x.event(
        "MATERIAL_ARRIVED", reel_id=reel_id, barcode=x.barcode, mode="BYPASS", ready_for_pick=True
    )


async def test_late_original_bin_result_uses_v2_target_after_upgrade(xray):
    x = xray
    x.decisions["put_target_decide"] = {
        "result": "ACCEPT",
        "target_locator": {"type": "TRANSIT_BIN"},
    }
    arrived = await outlet(x)
    await x.advance()
    assert await x.dispatch() == 1
    put = await x.dispatch_original_bin(arrived)
    assert put["task_type"] == "PUT_TO_BIN"
    original_target = put["params"]["target"]
    x.restart()

    current_target = {
        key: value for key, value in original_target.items() if key not in {"rack_id", "bin_id"}
    }
    await x.result(put, status=400, actual_qty=1, position="TARGET", actual_target=current_target)
    for _ in range(2):
        await x.result(put, actual_qty=1, position="TARGET", actual_target=original_target)
        await x.drain()

    (report,) = x.facts
    assert report["data"]["barcode"] == x.barcode
    assert report["data"]["to_locator"] == {"type": "TRANSIT_BIN"}
    assert len(x.commands) == 2


def replenish(x, rack="RACK-A", face="A"):
    return {
        "result": "REPLENISH_REQUIRED",
        "location_code": x.point,
        "incoming_rack": {"rack_id": rack, "target_face": face},
    }


def rack_slot(x, rack="RACK-A", face="A", **changes):
    return {
        "result": "ACCEPT",
        "target_locator": {
            "type": "RACK_SLOT",
            "location_code": x.point,
            "rack_id": rack,
            "rack_face": face,
            "slot_id": f"{face}-03",
            "rack_layer": 2,
            "rack_column": 3,
            **changes,
        },
        "reel_diameter": 13,
        "reel_thickness": 15.3,
    }


@pytest.mark.parametrize("fixed", [False, True], ids=["transport-rack", "fixed-test-rack"])
async def test_mock_wms_drives_counted_reels_to_rack_and_other_reels_to_transit(xray, fixed):
    x = xray
    targets = (
        rack_slot(x) | {"reel_diameter": 7, "reel_thickness": 8.1},
        rack_slot(x, slot_id="A-04", rack_column=4) | {"reel_diameter": 7, "reel_thickness": 9.2},
    )
    x.use_mock(*targets, fixed=fixed)
    for index, barcode in enumerate(MOCK_BARCODES, 3):
        reel_id = str(uuid4())
        await x.entry(barcode=barcode, reel_id=reel_id)
        await x.drain()
        first = x.commands[-1]
        assert first["params"]["decision"] == "COUNT_REQUIRED"
        await x.event(
            "XRAY_COUNT_COMPLETED",
            reel_id=reel_id,
            barcode=barcode,
            template_id=TEMPLATE,
            count_status="OK",
            qty=index,
        )
        await x.result(first, decision="COUNT_REQUIRED", qty=index)
        await x.drain()
        label_cmd = x.commands[-1]
        label = label_cmd["params"]["label"]["variables"]
        assert label["QQTYPC"] == str(index)
        await x.result(label_cmd, decision="OK", label_status="APPLIED")
        await x.event(
            "SCAN_COMPLETED",
            reel_id=reel_id,
            scan_stage="VERIFY",
            scan_status="OK",
            barcode=barcode,
            old_barcode=barcode,
            new_barcode=label["SPKG"],
        )
        await x.drain()
        await x.result(x.commands[-1], decision="OK", mode="COUNTED")
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=reel_id,
            barcode=label["SPKG"],
            new_barcode=label["SPKG"],
            mode="COUNTED",
            ready_for_pick=True,
        )
        await x.drain()
        if not fixed:
            (enter,) = x.transports
            if index == 3:
                assert x.commands[-1]["task_type"] == "APPLY_VERIFY_DECISION"
                await x.transport_result(enter)
                await x.drain()
        put = x.commands[-1]
        assert put["task_type"] == "PUT_TO_RACK"
        assert put["params"]["barcode"] == label["SPKG"]
        assert put["params"]["target"]["slot_id"] == f"A-0{index}"
        assert put["params"]["target"]["reel_thickness"] == targets[index - 3]["reel_thickness"]
        await placed(x, put)
        await x.drain()
        assert x.facts[-1]["data"]["to_locator"]["slot_id"] == f"A-0{index}"

    await outlet(x)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_BIN"
    await placed(x, put)
    await x.drain()
    assert x.facts[-1]["data"]["to_locator"] == {"type": "TRANSIT_BIN"}
    assert len(x.facts) == 3 and x.mock_facts == x.facts
    if fixed:
        assert not x.transports


@pytest.mark.parametrize(
    ("draw", "original", "part", "qty"),
    [
        (0, "001-original", None, None),
        (1, "001-original", "TEST-PART", 0),
        (9, MOCK_BARCODES[0], "TEST-PART-01", 612),
    ],
    ids=["bypass", "count-short-code", "count-full-code"],
)
async def test_random_mock_drives_both_modes_to_transit(
    xray, monkeypatch, draw, original, part, qty
):
    x = xray
    monkeypatch.setattr("app.businesses.xray.mock_wms.randbelow", lambda upper: draw, raising=False)
    async with x.use_random_mock(12.5) as restart_mock:
        await x.entry(barcode=original)
        await x.drain()
        (entry,) = x.commands
        barcode = original
        mode = "BYPASS"
        if qty is None:
            assert entry["params"]["decision"] == "BYPASS_COUNT"
            await x.result(entry, decision="BYPASS_COUNT", mode=mode)
        else:
            mode = "COUNTED"
            assert entry["params"]["decision"] == "COUNT_REQUIRED"
            assert entry["params"]["reel_thickness"] == 12.5
            await x.event(
                "XRAY_COUNT_COMPLETED",
                barcode=original,
                template_id=TEMPLATE,
                count_status="OK",
                qty=qty,
            )
            await x.result(entry, decision="COUNT_REQUIRED", qty=qty)
            await x.drain()
            label_cmd = x.commands[-1]
            label = label_cmd["params"]["label"]["variables"]
            assert label["PHHPN"] == part
            assert label["QQTYPC"] == str(qty)
            barcode = label["SPKG"]
            restart_mock()
            await x.result(label_cmd, decision="OK", label_status="APPLIED")
            await x.event(
                "SCAN_COMPLETED",
                scan_stage="VERIFY",
                scan_status="OK",
                barcode=original,
                old_barcode=original,
                new_barcode=barcode,
            )
            await x.drain()
            verify = x.commands[-1]
            assert verify["params"]["decision"] == "OK"
            await x.result(verify, decision="OK", mode=mode)
        arrival = {"new_barcode": barcode} if mode == "COUNTED" else {}
        await x.event(
            "MATERIAL_ARRIVED", barcode=barcode, mode=mode, ready_for_pick=True, **arrival
        )
        await x.drain()
        put = x.commands[-1]
        assert put["task_type"] == "PUT_TO_BIN"
        assert put["params"]["barcode"] == barcode
        await placed(x, put)
        await x.drain()
        (report,) = x.mock_facts
        assert report["data"]["barcode"] == barcode
        assert report["data"]["to_locator"] == {"type": "TRANSIT_BIN"}
        assert len(x.commands) == (2 if qty is None else 4)
        assert not x.transports


async def test_rack_enter_preserves_original_slot_authorization(xray):
    x = xray
    x.decisions["put_target_decide"] = rack_slot(x) | {"target_preparation": "REPLACE"}
    await outlet(x)
    await x.drain()
    (enter,) = x.transports
    assert enter["data"]["rack_id"] == "RACK-A"
    assert enter["data"]["target_face"] == "A"
    first = x.requests[-1]
    x.restart()
    await x.drain()
    assert x.requests[-1] == first
    assert len(x.transports) == 1

    await x.transport_result(enter)
    await x.drain()
    assert target_requests(x) == [first]
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    target = put["params"]["target"]
    assert target["location_id"] == "ECS-RACK-POINT"
    assert target["rack_id"] == "RACK-A"
    actual = {k: v for k, v in target.items() if not k.startswith("reel_")}
    await x.result(put, actual_qty=1, position="TARGET", actual_target=actual)
    await x.drain()
    assert x.facts[0]["data"]["to_locator"] == rack_slot(x)["target_locator"]
    assert x.facts[0]["data"]["barcode"] == first["data"]["barcode"]


def target_requests(x):
    return [r for r in x.requests if r["operation"] == "inbound.xray.put_target_decide@v1"]


async def ready_rack_put(x):
    x.decisions["put_target_decide"] = replenish(x)
    await outlet(x)
    await x.drain()
    x.decisions["put_target_decide"] = rack_slot(x)
    await x.transport_result(x.transports[-1])
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    return put


async def placed(x, put):
    actual = {k: v for k, v in put["params"]["target"].items() if not k.startswith("reel_")}
    await x.result(put, actual_qty=1, position="TARGET", actual_target=actual)


async def request_cancelable_rack_move(x):
    if x.rack_template == "CTU02":
        initial = await ready_rack_put(x)
        await placed(x, initial)
        await x.drain()
        x.decisions["put_target_decide"] = rack_slot(x, face="B") | {"target_preparation": "ROTATE"}
    else:
        x.decisions["put_target_decide"] = rack_slot(x) | {"target_preparation": "REPLACE"}
    reel = str(uuid4())
    arrived = await outlet(x, reel_id=reel)
    await x.drain()
    assert x.transports[-1]["data"]["rcs_template_id"] == x.rack_template
    return reel, arrived


async def test_concurrent_reels_reuse_one_enter_across_restart(xray):
    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL serializable transactions")
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    materials = {str(uuid4()): column for column in (3, 4)}

    def decide(request):
        column = int(request["data"]["barcode"])
        return rack_slot(x, slot_id=f"A-0{column}", rack_column=column) | {
            "target_preparation": "REPLACE"
        }

    x.decisions["put_target_decide"] = decide
    reels_by_event = {
        await x.entry(reel_id=reel, barcode=str(column)): reel for reel, column in materials.items()
    }
    await x.drain()
    for cmd in x.commands[:]:
        reel = reels_by_event[cmd["params"]["source_event_id"]]
        await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=reel,
            barcode=str(materials[reel]),
            mode="BYPASS",
            ready_for_pick=True,
        )
    await asyncio.gather(*(x.advance_reel(reel) for reel in materials))
    assert await x.dispatch() == 2
    await asyncio.gather(*(x.advance_reel(reel) for reel in materials))
    await x.drain()
    (enter,) = x.transports
    first = target_requests(x)
    assert len(first) == 2
    x.restart()
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert x.transports == [enter]
    assert len(target_requests(x)) == 2

    await x.transport_result(enter)
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.transports) == 1
    assert target_requests(x) == first
    (put,) = [c for c in x.commands if c["task_type"] == "PUT_TO_RACK"]
    await placed(x, put)
    await x.drain()
    puts = [c for c in x.commands if c["task_type"] == "PUT_TO_RACK"]
    assert len(puts) == 2
    assert {c["params"]["target"]["slot_id"] for c in puts} == {"A-03", "A-04"}
    assert target_requests(x) == first


@pytest.mark.parametrize("mode", ["ROTATE", "REPLACE"])
async def test_rack_move_waits_for_original_put_and_resumes(xray, mode):
    x = xray
    first_put = await ready_rack_put(x)
    rack = "RACK-B" if mode == "REPLACE" else "RACK-A"
    plan = rack_slot(x, rack=rack, face="B") | {"target_preparation": mode}
    x.decisions["put_target_decide"] = plan
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    source = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    original = target_requests(x)[-1]
    x.clock.advance(301)
    x.restart()
    await x.drain()
    assert len(x.transports) == 1
    assert not any(r["operation"] == "rack.departure_decide@v1" for r in x.requests)

    await placed(x, first_put)
    await x.drain()
    if mode == "ROTATE":
        _, move = x.transports
        assert move["data"]["kind"] == "RACK_ROTATE"
        assert move["data"]["rcs_template_id"] == "CK04"
        assert move["data"]["target_face"] == "B"
    else:
        (departure,) = [r for r in x.requests if r["operation"] == "rack.departure_decide@v1"]
        assert departure["data"]["current_face"] == "A"
        _, depart, *remaining = x.transports
        assert depart["data"]["rack_id"] == "RACK-A"
        assert depart["data"]["target"] == {"kind": "ZONE", "location_code": "RETURN-STOCK"}
        (move,) = remaining
        assert move["data"]["rack_id"] == "RACK-B"
        assert move["data"]["target_face"] == "B"
        # The departed rack's late result is independent of the new rack's physical proof.
        await x.transport_result(
            depart, final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"}
        )
    assert target_requests(x)[-1] == original
    x.restart()
    await x.drain()
    assert target_requests(x)[-1] == original

    await x.transport_result(move)
    await x.drain()
    assert target_requests(x)[-1] == original
    final_put = x.commands[-1]
    assert final_put["task_type"] == "PUT_TO_RACK"
    assert final_put["params"]["source_event_id"] == source
    assert final_put["params"]["target"]["rack_id"] == rack
    assert final_put["params"]["target"]["rack_face"] == "B"


@pytest.mark.parametrize("late_reply", [False, True])
async def test_conflicting_replacement_with_no_departure_releases_current_rack(xray, late_reply):
    x = xray
    first_put = await ready_rack_put(x)
    await placed(x, first_put)
    await x.drain()
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    x.decisions["departure_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    reel = str(uuid4())
    arrival = await outlet(x, reel_id=reel)
    await x.advance()
    assert await x.dispatch() == 1  # Put target decision.
    await x.advance()  # The original departure request is now durable.
    if not late_reply:
        assert await x.dispatch() == 1
        await x.advance()
        x.clock.advance(1)
        await x.advance()
        assert await x.dispatch() == 1
        await x.advance()
    await x.event(
        "MATERIAL_ARRIVED",
        event_id=None if late_reply else arrival,
        reel_id=reel,
        barcode="CONFLICT",
        mode="BYPASS",
        ready_for_pick=True,
    )
    if late_reply:
        await x.advance()
        assert await x.dispatch() == 1
    originals = [r for r in x.requests if r["operation"] == "rack.departure_decide@v1"]
    assert len(originals) == (1 if late_reply else 2)
    assert len(x.transports) == 1
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    x.clock.advance(600)
    x.restart()
    await x.drain()
    assert len(x.transports) == 1
    assert [r for r in x.requests if r["operation"] == "rack.departure_decide@v1"] == originals

    x.decisions["put_target_decide"] = rack_slot(x, slot_id="A-04", rack_column=4)
    healthy = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    assert put["params"]["source_event_id"] == healthy
    assert put["params"]["target"]["slot_id"] == "A-04"
    assert len(x.transports) == 1

    await placed(x, put)
    await x.drain()
    x.decisions["put_target_decide"] = rack_slot(x, face="B") | {"target_preparation": "ROTATE"}
    rotation_source = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    _, rotation = x.transports
    assert rotation["data"]["kind"] == "RACK_ROTATE"
    await x.transport_result(rotation)
    await x.drain()
    rotated_put = x.commands[-1]
    assert rotated_put["params"]["source_event_id"] == rotation_source
    assert rotated_put["params"]["target"]["rack_face"] == "B"
    await placed(x, rotated_put)
    await x.drain()

    x.clock.advance(1)
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert [move["data"]["rack_id"] for move in x.transports] == [
        "RACK-A",
        "RACK-A",
        "RACK-A",
        "RACK-B",
    ]
    *saved, renewed = [r for r in x.requests if r["operation"] == "rack.departure_decide@v1"]
    assert saved == originals
    assert renewed["operation_id"] not in {r["operation_id"] for r in originals}
    assert renewed["data"]["current_face"] == "B"


async def test_conflicting_replacement_after_departure_allows_new_plan_with_vacancy(xray):
    x = xray
    first_put = await ready_rack_put(x)
    await placed(x, first_put)
    await x.drain()
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    reel = str(uuid4())
    arrival = await outlet(x, reel_id=reel)
    for _ in range(3):
        await x.advance()
        assert await x.dispatch() == 1
    _, departure = x.transports
    assert departure["data"]["rack_id"] == "RACK-A"

    await x.event(
        "MATERIAL_ARRIVED",
        event_id=arrival,
        reel_id=reel,
        barcode="CONFLICT",
        mode="BYPASS",
        ready_for_pick=True,
    )
    x.clock.advance(600)
    x.restart()
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-C", face="B")
    healthy = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert len(x.transports) == 2
    assert x.commands[-1]["task_type"] == "APPLY_PKG_DECISION"

    await x.transport_result(
        departure, final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"}
    )
    await x.drain()
    assert len(x.transports) == 3
    incoming = x.transports[-1]
    assert incoming["data"]["rack_id"] == "RACK-C"
    await x.transport_result(incoming)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    assert put["params"]["source_event_id"] == healthy


async def test_conflicting_reel_retains_original_entry_until_result(xray):
    x = xray
    x.decisions["put_target_decide"] = rack_slot(x) | {"target_preparation": "REPLACE"}
    reel = str(uuid4())
    arrival = await outlet(x, reel_id=reel)
    await x.drain()
    (enter,) = x.transports
    await x.event(
        "MATERIAL_ARRIVED",
        event_id=arrival,
        reel_id=reel,
        barcode="CONFLICT",
        mode="BYPASS",
        ready_for_pick=True,
    )
    x.decisions["put_target_decide"] = rack_slot(x, slot_id="A-04", rack_column=4)
    healthy = await outlet(x, reel_id=str(uuid4()))
    x.clock.advance(600)
    x.restart()
    await x.drain()
    assert x.transports == [enter]
    assert x.commands[-1]["task_type"] == "APPLY_PKG_DECISION"

    await x.transport_result(enter)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    assert put["params"]["source_event_id"] == healthy
    await placed(x, put)
    await x.drain()

    x.decisions["put_target_decide"] = rack_slot(x, face="B") | {"target_preparation": "ROTATE"}
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert len(x.transports) == 2
    assert x.transports[-1]["data"]["kind"] == "RACK_ROTATE"


@pytest.mark.parametrize(
    ("xray", "settlement", "conflicted"),
    [
        ("F01", "away", False),
        ("F01", "away", True),
        ("F01", "at-point", True),
        ("F01", "unknown", True),
        ("F01", "newer-observation", True),
        ("CTU02", "away", False),
        ("CTU02", "at-point", True),
    ],
    indirect=["xray"],
)
async def test_reel_releases_rack_preparation_after_proven_nonarrival(xray, settlement, conflicted):
    x = xray
    reel, arrived = await request_cancelable_rack_move(x)
    original = x.transports[-1]
    original_requests = x.transports[:]
    original_puts = [command for command in x.commands if command["task_type"] == "PUT_TO_RACK"]
    if conflicted:
        await x.event(
            "MATERIAL_ARRIVED",
            event_id=arrived,
            reel_id=reel,
            barcode="CONFLICT",
            mode="BYPASS",
            ready_for_pick=True,
        )
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-C", face="B")
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert x.transports == original_requests

    if settlement == "newer-observation":
        await x.transport_result(original, outcome_revision=2, arrival_face="B")
    if settlement == "unknown":
        await x.transport_result(
            original,
            status="FAILED",
            failure_code="POSITION_UNKNOWN",
            position_unknown=True,
            final_position=None,
            arrival_face=None,
        )
    else:
        await x.transport_result(
            original,
            kind="RACK_MOVE",
            status="CANCELLED",
            final_position=(
                original["data"]["target"]
                if settlement == "at-point"
                else {"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"}
            ),
            arrival_face=None,
        )
    x.restart()
    await x.drain()
    assert x.transports == original_requests
    (job,) = await x.rack_jobs()
    assert (job.closure_reason == "NOT_ARRIVED" and not job.pending) is (settlement == "away")

    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    healthy = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    if settlement != "away":
        assert x.transports == original_requests
        assert x.commands[-1]["task_type"] == "APPLY_PKG_DECISION"
        return
    *preserved, replacement = x.transports
    assert preserved == original_requests
    assert replacement["data"]["rack_id"] == "RACK-B"
    assert replacement["operation_id"] != original["operation_id"]
    await x.transport_result(replacement)
    await x.drain()
    puts = [command for command in x.commands if command["task_type"] == "PUT_TO_RACK"]
    *prior_puts, put = puts
    assert prior_puts == original_puts
    assert put["params"]["source_event_id"] == healthy
    assert put["params"]["target"]["rack_id"] == "RACK-B"


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("commit_during_admission", [False, True])
async def test_late_cancelled_arrival_blocks_explicit_replacement(
    xray, monkeypatch, commit_during_admission
):
    x = xray
    if commit_during_admission and x.dialect != "postgresql":
        pytest.skip("requires concurrent PostgreSQL transactions")
    original_reel, arrived = await request_cancelable_rack_move(x)
    original = x.transports[-1]
    original_requests = x.transports[:]
    original_puts = [command for command in x.commands if command["task_type"] == "PUT_TO_RACK"]
    await x.event(
        "MATERIAL_ARRIVED",
        event_id=arrived,
        reel_id=original_reel,
        barcode="CONFLICT",
        mode="BYPASS",
        ready_for_pick=True,
    )
    await x.transport_result(
        original,
        kind="RACK_MOVE",
        status="CANCELLED",
        final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"},
        arrival_face=None,
    )
    await x.drain()
    assert x.transports == original_requests
    (closed,) = await x.rack_jobs()
    assert closed.closure_reason == "NOT_ARRIVED" and not closed.pending

    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    healthy_reel = str(uuid4())
    await outlet(x, reel_id=healthy_reel)
    await x.advance_reel(healthy_reel)
    assert await x.dispatch() == 1  # Save the healthy reel's WMS decision.

    if commit_during_admission:
        from app.businesses.xray.racks import XrayRacks

        original_prepare = XrayRacks._prepare_target

        async def prepare_after_arrival(self, *args, **kwargs):
            # The earlier position check has passed in this transaction's snapshot.
            await x.transport_result(original, outcome_revision=2)
            return await original_prepare(self, *args, **kwargs)

        monkeypatch.setattr(XrayRacks, "_prepare_target", prepare_after_arrival)
    else:
        await x.transport_result(original, outcome_revision=2)
    x.restart()
    reason = await x.advance_reel(healthy_reel)  # Before any background rack sweep.
    await x.dispatch()
    assert x.transports == original_requests
    assert reason == "RACK_POSITION_CONFLICT"
    await x.drain()
    assert x.transports == original_requests
    puts = [command for command in x.commands if command["task_type"] == "PUT_TO_RACK"]
    assert puts == original_puts
    x.restart()
    (protected,) = await x.rack_jobs()
    action = protected.actions[-1]
    assert protected.id == closed.id and protected.pending
    assert protected.review_reason == "POSITION_CONFLICT"
    assert protected.actual_position == original["data"]["target"]
    assert action.id == closed.current_action_id
    assert action.transport_task_id == original["data"]["transport_task_id"]
    assert action.basis["previous_not_arrived"] == closed.closure_basis
    assert (action.outcome["status"], action.outcome_revision) == ("CANCELLED", 1)
    assert (action.position_observation["status"], action.observation_revision) == ("SUCCEEDED", 2)


async def ready_replacement_after_cancelled_move(x):
    await request_cancelable_rack_move(x)
    original = x.transports[-1]
    await x.transport_result(
        original,
        kind="RACK_MOVE",
        status="CANCELLED",
        final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"},
        arrival_face=None,
    )
    await x.drain()
    (closed,) = await x.rack_jobs()
    assert closed.closure_reason == "NOT_ARRIVED" and not closed.pending

    allocation = rack_slot(x, rack="RACK-B", face="B")
    x.decisions["put_target_decide"] = allocation | {"target_preparation": "REPLACE"}
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    replacement = x.transports[-1]
    assert replacement["data"]["rack_id"] == "RACK-B"
    await x.transport_result(replacement)
    await x.drain()
    first_put = x.commands[-1]
    assert first_put["task_type"] == "PUT_TO_RACK"
    await placed(x, first_put)
    await x.drain()
    return original, closed, allocation


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
async def test_late_cancelled_arrival_blocks_put_to_already_ready_rack(xray, monkeypatch):
    from app.businesses.xray.racks import XrayRacks

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires concurrent PostgreSQL transactions")
    original, _, allocation = await ready_replacement_after_cancelled_move(x)

    x.decisions["put_target_decide"] = allocation
    reel = str(uuid4())
    await outlet(x, reel_id=reel)
    await x.advance_reel(reel)
    assert await x.dispatch() == 1
    existing_commands, existing_transports = x.commands[:], x.transports[:]

    rack_ready, arrival_saved = asyncio.Event(), asyncio.Event()
    original_target = XrayRacks.target

    async def pause_after_readiness(self, *args, **kwargs):
        result = await original_target(self, *args, **kwargs)
        if result[0] == "READY" and not rack_ready.is_set():
            rack_ready.set()
            await arrival_saved.wait()
        return result

    monkeypatch.setattr(XrayRacks, "target", pause_after_readiness)
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(x.advance_reel(reel))
        await rack_ready.wait()
        await x.transport_result(original, outcome_revision=2)
        arrival_saved.set()

    await x.dispatch()
    assert x.commands == existing_commands
    assert x.transports == existing_transports


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("after_gate", [False, True])
async def test_late_arrival_holds_queued_put_after_sender_restart(
    xray, callback_store, monkeypatch, projected, runtime_authority, after_gate
):
    from app.businesses.xray.models import Reel
    from app.businesses.xray.service import OWNER

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    x = xray
    original, closed, allocation = await ready_replacement_after_cancelled_move(x)
    x.decisions["put_target_decide"] = allocation
    reel = str(uuid4())
    await outlet(x, reel_id=reel)
    await x.advance_reel(reel)
    assert await x.dispatch() == 1
    existing_commands = x.commands[:]
    assert await x.advance_reel(reel) == "PUT_UNRESOLVED"
    assert x.commands == existing_commands
    async with callback_store() as session:
        saved = await session.scalar(select(Reel).where(Reel.reel_id == reel))
        code = saved.actions["OUTLET"]["command_code"]

    x.restart()
    await x.restart_sender()

    async def arrive():
        await x.transport_result(original, outcome_revision=2)
        if projected:
            await x.advance()
            protected = next(job for job in await x.rack_jobs() if job.id == closed.id)
            assert protected.pending and protected.review_reason == "POSITION_CONFLICT"

    if after_gate:
        before_send = MessageStore.before_send
        arrived = asyncio.Event()

        async def arrive_after_gate(store, claim, target):
            until = await before_send(store, claim, target)
            if claim.message.operation_id == code:
                assert until is not None
                await arrive()
                arrived.set()
            return until

        monkeypatch.setattr(MessageStore, "before_send", arrive_after_gate)
    else:
        await arrive()
    await x.dispatch()
    if after_gate:
        assert arrived.is_set()
    assert x.commands == existing_commands
    async with callback_store() as session:
        view = await app.state.ecs_commands.get_command(session, owner=OWNER, command_code=code)
        assert (view.delivery_status, view.hold_reason) == ("HELD", "RACK_POSITION_CONFLICT")


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_rack_put_keeps_arrival_evidence_locked_without_blocking_ecs_result(
    xray, callback_store, monkeypatch, runtime_authority
):
    from app.businesses.xray.service import OWNER
    from app.models import TransportTask
    from app.reliable.lifecycle import database_failure_code

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    original, _, allocation = await ready_replacement_after_cancelled_move(x)
    x.decisions["put_target_decide"] = allocation
    original_task = original["data"]["transport_task_id"]
    result_saved = asyncio.Event()

    async def check_wire(command):
        with pytest.raises(DBAPIError) as locked:
            async with callback_store.begin() as session:
                await session.scalar(
                    select(TransportTask.transport_task_id)
                    .where(TransportTask.transport_task_id == original_task)
                    .with_for_update(nowait=True)
                )
        assert database_failure_code(locked.value) == "55P03"
        # ECS is allowed to report the original result before returning the command ACK.
        await placed(x, command)
        result_saved.set()

    x.command_hooks["PUT_TO_RACK"] = check_wire
    reel = str(uuid4())
    await outlet(x, reel_id=reel)
    await x.advance_reel(reel)
    assert await x.dispatch() == 1
    assert await x.advance_reel(reel) == "PUT_UNRESOLVED"
    await x.dispatch()
    assert result_saved.is_set()
    async with callback_store() as session:
        view = await app.state.ecs_commands.get_command(
            session, owner=OWNER, command_code=x.commands[-1]["command_code"]
        )
        assert view.status == "SUCCEEDED"
    await x.transport_result(original, outcome_revision=2)


async def queue_put_after_cancelled_move(x, factory):
    from app.businesses.xray.models import Reel

    original, _, allocation = await ready_replacement_after_cancelled_move(x)
    x.decisions["put_target_decide"] = allocation
    reel = str(uuid4())
    await outlet(x, reel_id=reel)
    await x.advance_reel(reel)
    assert await x.dispatch() == 1
    assert await x.advance_reel(reel) == "PUT_UNRESOLVED"
    async with factory() as session:
        saved = await session.scalar(select(Reel).where(Reel.reel_id == reel))
        return original, saved.actions["OUTLET"]["command_code"]


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "connection_lost",
        "connection_lost_after_headers",
        "binding_changed",
        "lock_timeout",
        "pool_timeout",
        "pre_gate_pool_timeout",
    ],
)
async def test_rack_put_rechecks_send_permission_after_opening_attempt(
    xray, callback_store, monkeypatch, runtime_authority, fault
):
    from app.businesses.xray.service import OWNER, XrayService
    from app.models import ReliableDeliveryAttempt, ReliableMessage, TransportTask
    from app.reliable.lifecycle import EvidencePersistenceError

    x = xray
    if fault != "binding_changed" and x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    original, code = await queue_put_after_cancelled_move(x, callback_store)
    existing_commands = x.commands[:]
    await x.restart_sender()
    fault_applied = asyncio.Event()
    async with AsyncExitStack() as blockers:
        callbacks = await blockers.enter_async_context(asyncio.TaskGroup())
        with monkeypatch.context() as patch:
            if fault.startswith("connection_lost"):
                authorize = XrayService._authorize_rack_put
                guard_pid = None

                async def capture_pid(service, session, *, command):
                    nonlocal guard_pid
                    reason = await authorize(service, session, command=command)
                    if command.command_code == code:
                        guard_pid = await session.scalar(text("select pg_backend_pid()"))
                    return reason

                patch.setattr(XrayService, "_authorize_rack_put", capture_pid)

                async def disconnect():
                    assert guard_pid is not None
                    async with callback_store.begin() as session:
                        assert await session.scalar(
                            text("select pg_terminate_backend(:pid, 5000)"),
                            {"pid": guard_pid},
                        )
                    await x.transport_result(original, outcome_revision=2)
                    fault_applied.set()

                async def deliver_fault():
                    await asyncio.shield(callbacks.create_task(disconnect()))

                if fault == "connection_lost":
                    send = AsyncHttpClient.send

                    async def disconnect_before_send(client, request):
                        if json.loads(request.body).get("command_code") == code:
                            await deliver_fault()
                        return await send(client, request)

                    patch.setattr(AsyncHttpClient, "send", disconnect_before_send)
                else:
                    handle = httpx.AsyncHTTPTransport.handle_async_request

                    async def disconnect_before_body(transport, request):
                        if json.loads(request.content).get("command_code") == code:
                            original_trace = request.extensions.get("trace")

                            async def trace(event, info):
                                if event == "http11.send_request_body.started":
                                    await deliver_fault()
                                if original_trace is not None:
                                    await original_trace(event, info)

                            request.extensions["trace"] = trace
                        return await handle(transport, request)

                    patch.setattr(
                        httpx.AsyncHTTPTransport, "handle_async_request", disconnect_before_body
                    )
            elif fault in {"pool_timeout", "pre_gate_pool_timeout"}:
                from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

                original_guard = EcsLifecycle.http_guard
                source = callback_store.kw["bind"]
                engine = create_async_engine(
                    source.url,
                    execution_options=source.get_execution_options(),
                    pool_size=1,
                    max_overflow=0,
                    pool_timeout=0.1,
                )
                blockers.push_async_callback(engine.dispose)
                await blockers.enter_async_context(engine.connect())
                limited = async_sessionmaker(engine, expire_on_commit=False)

                def guard(service, factory, claim):
                    return original_guard(
                        service, limited if claim.message.operation_id == code else factory, claim
                    )

                if fault == "pool_timeout":
                    patch.setattr(EcsLifecycle, "http_guard", guard)
                else:
                    original_check = EcsLifecycle.check_dispatch

                    async def check(service, factory, claim, clock):
                        return await original_check(
                            service,
                            limited if claim.message.operation_id == code else factory,
                            claim,
                            clock,
                        )

                    patch.setattr(EcsLifecycle, "check_dispatch", check)
                fault_applied.set()
            else:
                before_send = MessageStore.before_send

                async def change_after_gate(store, claim, target):
                    until = await before_send(store, claim, target)
                    if claim.message.operation_id == code:
                        assert until is not None
                        if fault == "binding_changed":
                            await x.change_binding()
                        else:
                            session = await blockers.enter_async_context(callback_store.begin())
                            assert await session.scalar(
                                select(TransportTask.transport_task_id)
                                .where(
                                    TransportTask.transport_task_id
                                    == original["data"]["transport_task_id"]
                                )
                                .with_for_update()
                            )
                        fault_applied.set()
                    return until

                patch.setattr(MessageStore, "before_send", change_after_gate)
            if fault in {"connection_lost_after_headers", "pre_gate_pool_timeout"}:
                with pytest.raises(EvidencePersistenceError):
                    await x.dispatch()
            else:
                await x.dispatch()
    assert fault_applied.is_set()
    assert x.commands == existing_commands
    async with callback_store() as session:
        view = await app.state.ecs_commands.get_command(session, owner=OWNER, command_code=code)
        attempt = await session.scalar(
            select(ReliableDeliveryAttempt).where(ReliableDeliveryAttempt.operation_id == code)
        )
        if fault == "pre_gate_pool_timeout":
            assert attempt is None
            message = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation_id == code)
            )
            assert message.status == "SENDING" and message.lease_not_started
            assert not message.delivery_uncertain
        elif fault == "connection_lost_after_headers":
            assert attempt.attempt_state == "OPEN"
        else:
            assert attempt.attempt_state == "NOT_SENT"
        if fault == "binding_changed":
            assert (view.delivery_status, view.hold_reason) == ("HELD", "AUTHORIZATION_REVOKED")
            return
        assert view.delivery_status == (
            "SENDING" if fault == "connection_lost_after_headers" else "PENDING"
        )
    x.clock.advance(40)
    await x.restart_sender()
    await x.dispatch()
    if fault.startswith("connection_lost"):
        assert x.commands == existing_commands
        async with callback_store() as session:
            view = await app.state.ecs_commands.get_command(session, owner=OWNER, command_code=code)
            assert (view.delivery_status, view.hold_reason) == (
                "HELD",
                "LEASE_INTERRUPTED"
                if fault == "connection_lost_after_headers"
                else "RACK_POSITION_CONFLICT",
            )
    else:
        assert [command["command_code"] for command in x.commands[len(existing_commands) :]] == [
            code
        ]


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_rack_put_aborts_when_guard_disconnects_after_body_probe(
    xray, callback_store, monkeypatch, runtime_authority
):
    from httpcore._backends.anyio import AnyIOStream

    from app.businesses.xray.service import XrayService
    from app.reliable.lifecycle import EvidencePersistenceError

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    original, code = await queue_put_after_cancelled_move(x, callback_store)
    existing = x.commands[:]
    await x.restart_sender()
    body_ready = asyncio.Event()
    arrived = asyncio.Event()
    authorize = XrayService._authorize_rack_put
    write = AnyIOStream.write
    session_pid = None

    async def capture(service, session, *, command):
        nonlocal session_pid
        reason = await authorize(service, session, command=command)
        if command.command_code == code:
            session_pid = await session.scalar(text("select pg_backend_pid()"))
        return reason

    async def write_body(stream, data, *args, **kwargs):
        if code.encode() in data:
            body_ready.set()
            await arrived.wait()
        return await write(stream, data, *args, **kwargs)

    async def disconnect_and_arrive():
        async with asyncio.timeout(5):
            await body_ready.wait()
            async with callback_store.begin() as session:
                assert await session.scalar(
                    text("select pg_terminate_backend(:pid, 5000)"), {"pid": session_pid}
                )
            await x.transport_result(original, outcome_revision=2)
            arrived.set()

    monkeypatch.setattr(XrayService, "_authorize_rack_put", capture)
    monkeypatch.setattr(AnyIOStream, "write", write_body)
    async with asyncio.TaskGroup() as group:
        group.create_task(disconnect_and_arrive())
        with pytest.raises(EvidencePersistenceError):
            await x.dispatch()
    assert arrived.is_set()
    assert x.commands == existing


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_rack_put_expires_before_wire_write(xray, callback_store, monkeypatch, phase):
    from h11 import Data
    from h11 import Request as RequestHeaders
    from httpcore._async.http11 import AsyncHTTP11Connection

    from app.integrations.ecs_command.results import get_command

    x = xray
    _, code = await queue_put_after_cancelled_move(x, callback_store)
    existing = x.commands[:]
    x.clock.advance(299)
    await x.restart_sender()
    write_event = AsyncHTTP11Connection._send_event
    expired = False

    async def expire_before_write(connection, event, *args, **kwargs):
        nonlocal expired
        if (
            phase == "headers"
            and isinstance(event, RequestHeaders)
            and event.target == b"/api/v1/device/command"
        ) or (phase == "body" and isinstance(event, Data) and code.encode() in event.data):
            x.clock.advance(2)
            expired = True
        return await write_event(connection, event, *args, **kwargs)

    monkeypatch.setattr(AsyncHTTP11Connection, "_send_event", expire_before_write)
    await x.dispatch()
    assert expired
    assert x.commands == existing
    async with callback_store() as session:
        command = await get_command(session, owner_domain="xray", command_code=code)
        if phase == "body":
            assert command.delivery_status == "HELD"
            assert command.hold_reason == "DELIVERY_UNKNOWN"
        else:
            assert command.delivery_status == "EXPIRED"


@pytest.mark.parametrize("xray", ["CTU03"], indirect=True)
@pytest.mark.parametrize("late_result", [False, True])
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_explicit_replace_respects_ctu03_evidence_after_cleanup(
    xray, callback_store, monkeypatch, late_result, runtime_authority
):
    from datetime import UTC, datetime, timedelta

    from app.models import ReliableMessage, TransportTask
    from app.reliable.store import cleanup_terminal

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    x = xray
    inbound = await ready_rack_put(x)
    await placed(x, inbound)
    await x.drain()
    x.replies["ctu03_storage"] = True
    x.decisions["put_target_decide"] = {
        "result": "DEPART_REQUIRED",
        "location_code": x.point,
        "current_rack": {"rack_id": "RACK-A"},
    }
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    await outlet(x, reel_id=str(uuid4()))
    for _ in range(8):
        await x.advance()
        await x.dispatch()
        if len(x.transports) == 2:
            break
    _, departure = x.transports
    assert departure["data"]["rcs_template_id"] == "CTU03"
    x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 60_000}
    await x.drain()
    (closed,) = await x.rack_jobs()
    assert not closed.pending and closed.closure_reason == "RACK_ALREADY_IN_STORAGE"
    async with callback_store.begin() as session:
        identity = await session.scalar(
            select(ReliableMessage.id).where(
                ReliableMessage.operation_id == departure["operation_id"]
            )
        )
        assert (
            await cleanup_terminal(
                session,
                now=datetime.now(UTC) + timedelta(days=91),
                retention_days=90,
                candidate_ids=(identity,),
            )
            == 1
        )
    if late_result:
        await x.transport_result(
            departure,
            status="FAILED",
            failure_code="RCS_EXECUTION_FAILED",
            final_position={"kind": "RACK_POSITION", "location_code": x.point},
        )
        async with callback_store() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == departure["data"]["transport_task_id"]
                )
            )
            assert task.applied_outcome or task.pending_outcome
    x.restart()
    await x.restart_sender()
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE",
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    if late_result:
        assert len(x.transports) == 2
    else:
        assert len(x.transports) == 3
        entry = x.transports[-1]["data"]
        assert entry["rack_id"] == "RACK-B" and entry["target"]["location_code"] == x.point


async def test_replacement_waits_for_an_authorized_plate_while_arm_is_busy(xray):
    x = xray
    initial = await ready_rack_put(x)
    await placed(x, initial)
    await x.drain()
    x.decisions["put_target_decide"] = {
        "result": "ACCEPT",
        "target_locator": {"type": "TRANSIT_BIN"},
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    transit = x.commands[-1]
    assert transit["task_type"] == "PUT_TO_BIN"

    x.decisions["put_target_decide"] = rack_slot(x, slot_id="A-04", rack_column=4)
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE"
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert len(x.transports) == 1
    assert not any(r["operation"] == "rack.departure_decide@v1" for r in x.requests)

    await placed(x, transit)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    assert put["params"]["target"]["slot_id"] == "A-04"
    assert not any(r["operation"] == "rack.departure_decide@v1" for r in x.requests)


@pytest.mark.parametrize("plan", ["unknown", "wrong-point", "wrong-face-result", "missing-face"])
async def test_unproved_rack_never_allows_put_or_second_enter(xray, plan):
    x = xray
    if plan == "unknown":
        x.decisions["put_target_decide"] = rack_slot(x)
    else:
        x.decisions["put_target_decide"] = replenish(x)
        if plan == "wrong-point":
            x.decisions["put_target_decide"]["location_code"] = "ANOTHER-WORKLINE"
    await outlet(x)
    await x.drain()
    entered = plan in {"wrong-face-result", "missing-face"}
    if entered:
        (enter,) = x.transports
        await x.transport_result(enter, arrival_face="B" if plan == "wrong-face-result" else None)
    x.restart()
    await x.drain()
    assert len(x.transports) == int(entered)
    assert len(target_requests(x)) == 1
    assert not any(c["task_type"] == "PUT_TO_RACK" for c in x.commands)


async def test_known_occupied_point_rejects_independent_replenishment(xray):
    x = xray
    first = await ready_rack_put(x)
    await placed(x, first)
    await x.drain()
    x.decisions["put_target_decide"] = replenish(x, rack="RACK-B", face="B")
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert len(x.transports) == 1
    assert len([c for c in x.commands if c["task_type"] == "PUT_TO_RACK"]) == 1


async def test_binding_change_fences_original_replenish_decision(xray):
    x = xray
    x.decisions["put_target_decide"] = replenish(x)
    await outlet(x)
    await x.advance()
    assert await x.dispatch() == 1
    assert len(target_requests(x)) == 1
    await x.change_binding()
    x.restart()
    await x.drain()
    assert not x.transports
    assert len(target_requests(x)) == 1


async def test_pending_pages_skip_future_wait_and_review_and_reuse_originals(xray):
    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL concurrent progression")
    notices, tick = x.wakeups(page_size=3)
    wait_ids = [str(uuid4()) for _ in range(4)]
    x.decisions["pkg_decide"] = {"result": "WAIT", "retry_after_ms": 60_000}
    entry_ids = {
        identity: await x.entry(reel_id=identity, barcode=f"WAIT-{identity}")
        for identity in wait_ids
    }
    for _ in range(4):
        identity = str(uuid4())
        event = await x.entry(reel_id=identity)
        await x.entry(reel_id=identity, event_id=event, barcode="CONFLICTING")
    await x.drain()
    old_requests = list(x.requests)
    assert {r["data"]["barcode"] for r in old_requests} == {f"WAIT-{i}" for i in wait_ids}
    assert not x.commands

    notices.clear()
    ready_ids = [str(uuid4()) for _ in range(7)]
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    for identity in ready_ids:
        await x.entry(reel_id=identity, barcode=f"READY-{identity}")
    assert notices == ["xray"] * len(ready_ids)
    # The worker must cross three pages without opening transactions for sleeping reels.
    assert await tick(notices[0]) == len(ready_ids)
    await asyncio.gather(tick(notices[0]), tick())
    while await x.dispatch():
        pass
    assert x.requests[: len(old_requests)] == old_requests
    assert sorted(r["data"]["barcode"] for r in x.requests[len(old_requests) :]) == sorted(
        f"READY-{identity}" for identity in ready_ids
    )
    await asyncio.gather(tick(notices[-1]), tick())
    while await x.dispatch():
        pass
    assert sorted(c["params"]["barcode"] for c in x.commands) == sorted(
        f"READY-{identity}" for identity in ready_ids
    )
    await tick()
    assert await x.dispatch() == 0

    # A new original device fact can make a WAIT reel eligible before its WMS deadline.
    request_id = await x.event(
        "XRAY_NG_REQUESTED",
        reel_id=wait_ids[0],
        source_event_id=entry_ids[wait_ids[0]],
        reason_code="NO_CAPACITY",
        reason_message="cannot wait",
    )
    await tick(notices[-1])
    await x.dispatch()
    ng = x.commands[-1]
    assert ng["params"]["decision"] == "NG"
    assert ng["params"]["source_event_id"] == request_id
    assert len(x.requests) == len(old_requests) + len(ready_ids)

    # The periodic invocation discovers due waits even without a fresh callback.
    notices.clear()
    x.clock.advance(60)
    await tick()
    while await x.dispatch():
        pass
    renewed = x.requests[len(old_requests) + len(ready_ids) :]
    assert {r["data"]["barcode"] for r in renewed} == {f"WAIT-{i}" for i in wait_ids[1:]}
    assert all(
        r["operation_id"] not in {old["operation_id"] for old in old_requests} for r in renewed
    )


async def test_wait_keeps_original_unsettled_command_eligible(xray):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED", "reel_thickness": 8.1},
        count_decide={"result": "WAIT", "retry_after_ms": 60_000},
    )
    await x.entry()
    await x.drain()
    (count,) = x.commands
    await x.event(
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, template_id=TEMPLATE, count_status="OK", qty=1
    )
    await x.drain()
    assert await x.advance() == 1
    await x.result(count, decision="COUNT_REQUIRED", qty=0)
    assert await x.advance() == 1
    assert await x.advance() == 0

    x.clock.advance(60)
    await x.drain()
    assert len(x.requests) == 2
    assert x.commands == [count]


async def test_departure_wait_reuses_original_and_implicit_entry_requires_vacancy(xray):
    x = xray
    first = await ready_rack_put(x)
    await placed(x, first)
    await x.drain()
    x.decisions["put_target_decide"] = {
        "result": "DEPART_REQUIRED",
        "location_code": x.point,
        "current_rack": {"rack_id": "RACK-A"},
    }
    x.decisions["departure_decide"] = {"result": "WAIT", "retry_after_ms": 10_000}
    await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    assert len(x.transports) == 1
    waiting = list(x.requests)
    x.clock.advance(9.999)
    x.restart()
    await x.drain()
    assert x.requests == waiting
    x.clock.advance(0.001)
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    await x.drain()
    assert len(x.transports) == 2
    original, renewed = [r for r in x.requests if r["operation"] == "rack.departure_decide@v1"]
    assert original["operation_id"] != renewed["operation_id"]
    assert original["data"] == renewed["data"]

    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B")
    x.clock.advance(1)
    await x.drain()
    assert len(x.transports) == 2  # Departure submission alone does not prove vacancy.
    target = target_requests(x)[-1]

    await x.transport_result(
        x.transports[-1], final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"}
    )
    await x.drain()
    assert len(x.transports) == 3
    assert x.transports[-1]["data"]["rack_id"] == "RACK-B"
    await x.transport_result(x.transports[-1])
    await x.drain()
    assert target_requests(x)[-1] == target
    assert x.commands[-1]["task_type"] == "PUT_TO_RACK"


@pytest.mark.parametrize("xray", ["CTU03"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("after_gate", [False, True])
async def test_ctu03_late_result_fences_queued_explicit_replace(
    xray, callback_store, monkeypatch, runtime_authority, after_gate
):
    from datetime import UTC, datetime, timedelta

    from app.businesses._components.rack_dispatch.models import RackAction, RackJob
    from app.models import ReliableMessage, TransportTask
    from app.reliable.store import cleanup_terminal

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    x = xray
    inbound = await ready_rack_put(x)
    await placed(x, inbound)
    await x.drain()
    x.replies["ctu03_storage"] = True
    x.decisions["put_target_decide"] = {
        "result": "DEPART_REQUIRED",
        "location_code": x.point,
        "current_rack": {"rack_id": "RACK-A"},
    }
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    await outlet(x, reel_id=str(uuid4()))
    for _ in range(8):
        await x.advance()
        await x.dispatch()
        if len(x.transports) == 2:
            break
    _, departure = x.transports
    assert departure["data"]["rcs_template_id"] == "CTU03"
    x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 60_000}
    await x.drain()
    (closed,) = await x.rack_jobs()
    assert not closed.pending and closed.closure_reason == "RACK_ALREADY_IN_STORAGE"
    async with callback_store.begin() as session:
        identity = await session.scalar(
            select(ReliableMessage.id).where(
                ReliableMessage.operation_id == departure["operation_id"]
            )
        )
        assert (
            await cleanup_terminal(
                session,
                now=datetime.now(UTC) + timedelta(days=91),
                retention_days=90,
                candidate_ids=(identity,),
            )
            == 1
        )

    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE",
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.advance()
    await x.dispatch()
    for _ in range(4):
        await x.advance()
        async with callback_store() as session:
            queued = (
                await session.execute(
                    select(RackAction.submit_operation_id, ReliableMessage.status)
                    .join(RackJob, RackJob.id == RackAction.job_id)
                    .join(ReliableMessage, ReliableMessage.id == RackAction.submit_message_id)
                    .where(RackJob.rack_id == "RACK-B", RackAction.kind == "ENTER")
                )
            ).one_or_none()
        if queued:
            break
    assert queued and queued.status == "PENDING"
    assert len(x.transports) == 2

    async def late_result():
        await x.transport_result(
            departure,
            status="FAILED",
            failure_code="RCS_EXECUTION_FAILED",
            final_position={"kind": "RACK_POSITION", "location_code": x.point},
        )
        async with callback_store() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == departure["data"]["transport_task_id"]
                )
            )
            assert task.pending_outcome or task.applied_outcome

    if after_gate:
        gate = MessageStore.before_send

        async def arrive_after_gate(store, claim, target):
            until = await gate(store, claim, target)
            if until is not None and claim.message.operation_id == queued.submit_operation_id:
                await late_result()
            return until

        monkeypatch.setattr(MessageStore, "before_send", arrive_after_gate)
    else:
        await late_result()
    x.restart()
    await x.restart_sender()
    await x.drain()
    assert not [req for req in x.transports if req["operation_id"] == queued.submit_operation_id]
    async with callback_store() as session:
        submission = await session.scalar(
            select(ReliableMessage).where(
                ReliableMessage.operation_id == queued.submit_operation_id
            )
        )
        assert (submission.status, submission.pause_reason) == ("STALLED", "RACK_POSITION_CONFLICT")


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_rack_put_blocks_when_db_disconnect_notice_is_delayed(
    xray, callback_store, monkeypatch, runtime_authority
):
    from contextlib import suppress

    from httpcore._backends.anyio import AnyIOStream

    from app.businesses.xray.service import XrayService
    from app.reliable.lifecycle import EvidencePersistenceError

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    original, code = await queue_put_after_cancelled_move(x, callback_store)
    existing = x.commands[:]
    await x.restart_sender()
    body_ready = asyncio.Event()
    arrived = asyncio.Event()
    written = asyncio.Event()
    authorize = XrayService._authorize_rack_put
    write = AnyIOStream.write
    raw = None
    pid = None

    async def peer_received(command):
        if command["command_code"] == code:
            written.set()

    x.command_hooks["PUT_TO_RACK"] = peer_received

    async def capture(service, session, *, command):
        nonlocal raw, pid
        reason = await authorize(service, session, command=command)
        if command.command_code == code:
            db = await session.connection()
            raw = (await db.get_raw_connection()).driver_connection
            pid = raw.get_server_pid()
        return reason

    async def body_with_delayed_db_notice(stream, data, *args, **kwargs):
        if code.encode() not in data:
            return await write(stream, data, *args, **kwargs)
        assert raw is not None
        # Delay only the lock holder’s notification; the callback uses its own connection.
        raw._transport.pause_reading()
        try:
            body_ready.set()
            await arrived.wait()
            await write(stream, data, *args, **kwargs)
            # Keep the database notification delayed until the write is rejected or
            # a complete request reaches the peer.
            with suppress(TimeoutError):
                async with asyncio.timeout(0.2):
                    await written.wait()
        finally:
            raw._transport.resume_reading()

    async def disconnect_and_arrive():
        async with asyncio.timeout(5):
            await body_ready.wait()
            async with callback_store.begin() as session:
                assert await session.scalar(
                    text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid}
                )
            await x.transport_result(original, outcome_revision=2)
            arrived.set()

    monkeypatch.setattr(XrayService, "_authorize_rack_put", capture)
    monkeypatch.setattr(AnyIOStream, "write", body_with_delayed_db_notice)
    async with asyncio.TaskGroup() as group:
        group.create_task(disconnect_and_arrive())
        try:
            await x.dispatch()
        except EvidencePersistenceError:
            pass
    assert arrived.is_set()
    assert x.commands == existing


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
async def test_rack_put_checks_deadline_when_write_resumes_before_timer(
    xray, callback_store, monkeypatch
):
    import time
    from datetime import timedelta

    from app.http_client.client import _GuardedSocketStream

    x = xray
    _, code = await queue_put_after_cancelled_move(x, callback_store)
    existing = x.commands[:]
    await x.restart_sender()
    loop = asyncio.get_running_loop()
    body_due = None
    expired = False
    gate = MessageStore.before_send
    send = _GuardedSocketStream.send

    async def shorter_lease(store, claim, target):
        nonlocal body_due
        until = await gate(store, claim, target)
        if until is not None and claim.message.operation_id == code:
            body_due = loop.time() + 0.35
            return x.clock() + timedelta(seconds=0.35)
        return until

    def stall_other_callback():
        nonlocal expired
        time.sleep(0.5)
        assert loop.time() > body_due
        x.clock.advance(1)
        expired = True

    async def checkpoint_after_stall(stream, item):
        if code.encode() in item:
            loop.call_soon(stall_other_callback)
        await send(stream, item)

    monkeypatch.setattr(MessageStore, "before_send", shorter_lease)
    monkeypatch.setattr(_GuardedSocketStream, "send", checkpoint_after_stall)
    await x.dispatch()
    assert expired
    assert x.commands == existing


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize("fault", ["timeout", "disconnect"])
async def test_pending_rack_put_is_fenced_before_socket_retry(
    xray, callback_store, monkeypatch, fault
):
    import socket
    import time
    from contextlib import suppress
    from datetime import timedelta
    from urllib.parse import urlsplit

    from app.businesses.xray.service import XrayService
    from app.http_client import client as http_client
    from app.reliable.lifecycle import EvidencePersistenceError

    x = xray
    if fault == "disconnect" and x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    original, code = await queue_put_after_cancelled_move(x, callback_store)
    prior = x.commands[:]
    await x.restart_sender()
    peer_port = urlsplit(x.ecs_origin).port
    raw = None
    pid = None
    expires_at = None
    blocked_writer = None
    waiting = asyncio.Event()
    writable = asyncio.Event()
    peer_received = asyncio.Event()
    loop = asyncio.get_running_loop()
    authorize = XrayService._authorize_rack_put
    send = socket.socket.send
    wait_writable = http_client.wait_writable
    gate = MessageStore.before_send

    async def received(command):
        if command["command_code"] == code:
            peer_received.set()

    x.command_hooks["PUT_TO_RACK"] = received

    async def capture(service, session, *, command):
        nonlocal raw, pid
        result = await authorize(service, session, command=command)
        if command.command_code == code and fault == "disconnect":
            db = await session.connection()
            raw = (await db.get_raw_connection()).driver_connection
            pid = raw.get_server_pid()
        return result

    async def short_lease(store, claim, target):
        nonlocal expires_at
        until = await gate(store, claim, target)
        if fault == "timeout" and until is not None and claim.message.operation_id == code:
            expires_at = loop.time() + 0.4
            return x.clock() + timedelta(seconds=0.4)
        return until

    def simulate_backpressure(writer, data, *args, **kwargs):
        nonlocal blocked_writer
        if (
            blocked_writer is None
            and code.encode() in bytes(data)
            and writer.getpeername()[1] == peer_port
        ):
            blocked_writer = writer
            raise BlockingIOError
        return send(writer, data, *args, **kwargs)

    async def wait_for_socket(writer):
        if writer is blocked_writer:
            waiting.set()
            await writable.wait()
        else:
            await wait_writable(writer)

    def stall_before_resuming():
        time.sleep(0.5)
        x.clock.advance(1)
        assert loop.time() > expires_at

    async def fail_before_socket_is_writable():
        async with asyncio.timeout(5):
            await waiting.wait()
            if fault == "disconnect":
                raw._transport.pause_reading()
            try:
                if fault == "disconnect":
                    async with callback_store.begin() as session:
                        assert await session.scalar(
                            text("select pg_terminate_backend(:pid, 5000)"), {"pid": pid}
                        )
                    await x.transport_result(original, outcome_revision=2)
                else:
                    # The writable task resumes before the event loop runs the expired timer.
                    loop.call_soon(stall_before_resuming)
                writable.set()
                with suppress(TimeoutError):
                    async with asyncio.timeout(0.15):
                        await peer_received.wait()
            finally:
                if fault == "disconnect":
                    raw._transport.resume_reading()

    monkeypatch.setattr(XrayService, "_authorize_rack_put", capture)
    monkeypatch.setattr(MessageStore, "before_send", short_lease)
    monkeypatch.setattr(socket.socket, "send", simulate_backpressure)
    monkeypatch.setattr(http_client, "wait_writable", wait_for_socket)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(fail_before_socket_is_writable())
        with suppress(EvidencePersistenceError):
            await x.dispatch()
    assert waiting.is_set() and writable.is_set()
    assert not peer_received.is_set()
    assert x.commands == prior


@pytest.mark.parametrize("xray", ["CTU03"], indirect=True)
@pytest.mark.parametrize("other_owner", [False, True])
async def test_ctu03_gate_locks_original_after_point_reassignment(
    xray, callback_store, monkeypatch, other_owner
):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete, insert, update

    from app.businesses._components.rack_dispatch.models import RackAction, RackJob
    from app.integrations.wms_transport.dispatch import TransportDispatch
    from app.models import ReliableMessage, TransportTask
    from app.reliable.store import cleanup_terminal

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    inbound = await ready_rack_put(x)
    await placed(x, inbound)
    await x.drain()
    x.replies["ctu03_storage"] = True
    x.decisions["put_target_decide"] = {
        "result": "DEPART_REQUIRED",
        "location_code": x.point,
        "current_rack": {"rack_id": "RACK-A"},
    }
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    await outlet(x, reel_id=str(uuid4()))
    for _ in range(8):
        await x.advance()
        await x.dispatch()
        if len(x.transports) == 2:
            break
    _, departure = x.transports
    assert departure["data"]["rcs_template_id"] == "CTU03"
    x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 60_000}
    await x.drain()
    (closed,) = await x.rack_jobs()
    assert not closed.pending and closed.closure_reason == "RACK_ALREADY_IN_STORAGE"
    async with callback_store.begin() as session:
        identity = await session.scalar(
            select(ReliableMessage.id).where(
                ReliableMessage.operation_id == departure["operation_id"]
            )
        )
        assert (
            await cleanup_terminal(
                session,
                now=datetime.now(UTC) + timedelta(days=91),
                retention_days=90,
                candidate_ids=(identity,),
            )
            == 1
        )

    if other_owner:
        # Seed the closed predecessor as a retained Roller row from the prior point assignment.
        async with callback_store.begin() as session:
            job = await session.get(RackJob, closed.id)
            old = WorkLine(
                line_code=f"OLD-{uuid4().hex}",
                line_name="Old Roller line",
                business_code="smt_manual",
                is_enabled=False,
            )
            session.add(old)
            await session.flush()
            rows = list(
                (
                    await session.scalars(
                        select(RackAction)
                        .where(RackAction.job_id == job.id)
                        .order_by(RackAction.ordinal)
                    )
                ).all()
            )
            actions = [
                {c.key: getattr(action, c.key) for c in RackAction.__table__.columns}
                for action in rows
            ]
            origin = {c.key: getattr(job, c.key) for c in RackJob.__table__.columns}
            for action in reversed(actions):
                await session.execute(delete(RackAction).where(RackAction.id == action["id"]))
            await session.execute(delete(RackJob).where(RackJob.id == closed.id))
            origin.update(
                owner_domain="smt_manual", work_line_id=old.id, source_kind="ROLLER_ORDINARY"
            )
            await session.execute(insert(RackJob).values(origin))
            for action in actions:
                action.update(owner_domain="smt_manual", work_line_id=old.id)
                await session.execute(insert(RackAction).values(action))
            await session.execute(
                update(TransportTask)
                .where(
                    TransportTask.transport_task_id.in_(
                        [
                            action["transport_task_id"]
                            for action in actions
                            if action["transport_task_id"]
                        ]
                    )
                )
                .values(owner_domain="smt_manual")
            )
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE",
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.advance()
    await x.dispatch()
    for _ in range(4):
        await x.advance()
        async with callback_store() as session:
            queued = (
                await session.execute(
                    select(RackAction.submit_operation_id, ReliableMessage.status)
                    .join(RackJob, RackJob.id == RackAction.job_id)
                    .join(ReliableMessage, ReliableMessage.id == RackAction.submit_message_id)
                    .where(RackJob.rack_id == "RACK-B", RackAction.kind == "ENTER")
                )
            ).one_or_none()
        if queued:
            break
    assert queued and queued.status == "PENDING"
    assert len(x.transports) == 2

    async def late_result():
        await x.transport_result(
            departure,
            status="FAILED",
            failure_code="RCS_EXECUTION_FAILED",
            final_position={"kind": "RACK_POSITION", "location_code": x.point},
        )
        async with callback_store() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == departure["data"]["transport_task_id"]
                )
            )
            assert task.pending_outcome or task.applied_outcome

    original_guard = TransportDispatch.http_guard
    late_committed_before_send = False
    guard_entered = False

    @asynccontextmanager
    async def inject_after_position_authorized(guard, factory, claim):
        nonlocal late_committed_before_send, guard_entered
        async with original_guard(guard, factory, claim) as auth:
            if claim.message.operation_id == queued.submit_operation_id:
                assert auth[0] is None and auth[1] is not None
                guard_entered = True
                # Only a real row lock may defer this independently owned callback.
                try:
                    async with asyncio.timeout(0.4):
                        await late_result()
                except TimeoutError:
                    pass
                else:
                    late_committed_before_send = True
            yield auth

    monkeypatch.setattr(TransportDispatch, "http_guard", inject_after_position_authorized)
    x.restart()
    await x.restart_sender()
    await x.drain()
    assert guard_entered
    sent = any(req["operation_id"] == queued.submit_operation_id for req in x.transports)
    assert sent
    assert not late_committed_before_send
    await late_result()


@asynccontextmanager
async def database_relay(factory):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    source = factory.kw["bind"]
    forward = asyncio.Event()
    forward.set()
    accepted = set()

    async def serve(client_reader, client_writer):
        task = asyncio.current_task()
        accepted.add(task)
        remote_writer = None
        pumps = ()
        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                source.url.host, source.url.port or 5432
            )

            async def transfer(reader, writer, downstream=False):
                while True:
                    chunk = await reader.read(65536)
                    if downstream:
                        await forward.wait()
                    if not chunk:
                        return
                    writer.write(chunk)
                    await writer.drain()

            pumps = (
                asyncio.create_task(transfer(client_reader, remote_writer)),
                asyncio.create_task(transfer(remote_reader, client_writer, downstream=True)),
            )
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            client_writer.close()
            if remote_writer is not None:
                remote_writer.close()
            accepted.discard(task)

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        engine = create_async_engine(
            source.url.set(host="127.0.0.1", port=server.sockets[0].getsockname()[1]),
            execution_options=source.get_execution_options(),
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False), forward
        finally:
            forward.set()
            await engine.dispose()
            active = tuple(accepted)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)


def pause_after_partial_body(monkeypatch, *, identity, peer, after_probe=False):
    import socket
    from urllib.parse import urlsplit

    marker = identity.encode()
    peer_port = urlsplit(peer).port
    native_send = socket.socket.send
    partial = asyncio.Event()
    paused = asyncio.Event()
    release = asyncio.Event()

    def send_prefix(writer, data, *args, **kwargs):
        if not partial.is_set() and marker in bytes(data) and writer.getpeername()[1] == peer_port:
            assert native_send(writer, data[:8], *args, **kwargs) == 8
            partial.set()
            return 8
        return native_send(writer, data, *args, **kwargs)

    async def pause():
        paused.set()
        await release.wait()

    async def probe(original):
        applies = partial.is_set() and not paused.is_set()
        if applies and not after_probe:
            await pause()
        await original()
        if applies and after_probe:
            await pause()

    monkeypatch.setattr(socket.socket, "send", send_prefix)
    return paused, release, probe


@pytest.mark.parametrize("xray", ["CTU01", "CTU02"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "known_disconnect",
        "silent_disconnect",
        "after_probe_disconnect",
        "deadline",
        "wall_clock_step",
        "short_only",
    ],
)
async def test_short_rack_put_does_not_restart_after_partial_write(
    xray, callback_store, monkeypatch, fault
):
    import time
    from contextlib import AsyncExitStack, suppress
    from datetime import timedelta

    from app.businesses.xray.service import XrayService
    from app.integrations.ecs_command.results import get_command
    from app.reliable.lifecycle import EvidencePersistenceError

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("review uses real PostgreSQL")
    original, code = await queue_put_after_cancelled_move(x, callback_store)
    prior = x.commands[:]
    await x.restart_sender()
    authorize = XrayService._authorize_rack_put
    gate = MessageStore.before_send
    pg_pid = None
    raw = None
    written = asyncio.Event()
    fault_injected = asyncio.Event()

    async def received(command):
        if command["command_code"] == code:
            written.set()

    x.command_hooks["PUT_TO_RACK"] = received

    async def capture(service, session, *, command):
        nonlocal pg_pid, raw
        reason = await authorize(service, session, command=command)
        if command.command_code == code:
            connection = await session.connection()
            raw = (await connection.get_raw_connection()).driver_connection
            pg_pid = raw.get_server_pid()
        return reason

    async def short_lease(store, claim, target):
        until = await gate(store, claim, target)
        if until is not None and claim.message.operation_id == code:
            if fault == "deadline":
                return x.clock() + timedelta(seconds=0.6)
            if fault == "wall_clock_step":
                return x.clock() + timedelta(seconds=2)
        return until

    monkeypatch.setattr(XrayService, "_authorize_rack_put", capture)
    monkeypatch.setattr(MessageStore, "before_send", short_lease)
    waiting, release, probe = pause_after_partial_body(
        monkeypatch,
        identity=code,
        peer=x.ecs_origin,
        after_probe=fault
        in {"after_probe_disconnect", "deadline", "wall_clock_step", "short_only"},
    )
    async with AsyncExitStack() as stack:
        relay_factory = forward = None
        if fault in {"silent_disconnect", "after_probe_disconnect"}:
            relay_factory, forward = await stack.enter_async_context(database_relay(callback_store))
        open_guard = EcsLifecycle.http_guard

        @asynccontextmanager
        async def route(service, factory, claim):
            applies = claim.message.operation_id == code
            async with open_guard(
                service, (relay_factory or factory) if applies else factory, claim
            ) as auth:
                if not applies:
                    yield auth
                    return

                async def check():
                    await probe(auth[1])

                yield auth[0], check, auth[2], auth[3]

        monkeypatch.setattr(EcsLifecycle, "http_guard", route)

        async def fail_while_body_partial():
            async with asyncio.timeout(5):
                await waiting.wait()
                if fault == "known_disconnect":
                    raw._transport.pause_reading()
                elif forward is not None:
                    forward.clear()
                try:
                    if fault.endswith("disconnect"):
                        async with callback_store.begin() as session:
                            assert await session.scalar(
                                text("select pg_terminate_backend(:pid, 5000)"), {"pid": pg_pid}
                            )
                        await x.transport_result(original, outcome_revision=2)
                    elif fault == "deadline":
                        asyncio.get_running_loop().call_soon(time.sleep, 0.7)
                        x.clock.advance(1)
                    elif fault == "wall_clock_step":
                        # Advance UTC only. The process's monotonic write timer has not elapsed.
                        x.clock.advance(3)
                    fault_injected.set()
                    release.set()
                    with suppress(TimeoutError):
                        async with asyncio.timeout(0.3):
                            await written.wait()
                finally:
                    if fault == "known_disconnect":
                        raw._transport.resume_reading()
                    elif forward is not None:
                        forward.set()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(fail_while_body_partial())
            with suppress(EvidencePersistenceError):
                await x.dispatch()
    assert fault_injected.is_set()
    assert x.commands == prior
    await x.restart_sender()
    for _ in range(2):
        x.clock.advance(40)
        await x.dispatch()
    assert x.commands == prior
    if fault in {"deadline", "wall_clock_step", "short_only"}:
        async with callback_store() as session:
            command = await get_command(session, owner_domain="xray", command_code=code)
            assert command.delivery_status == "HELD"
            assert command.hold_reason == "DELIVERY_UNKNOWN"


@pytest.mark.parametrize("xray", ["CTU03"], indirect=True)
@pytest.mark.parametrize("fault", ["short_only", "delayed_result", "already_reported"])
async def test_ctu03_restart_after_partial_entry(xray, callback_store, monkeypatch, fault):
    from contextlib import suppress
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete, insert, update

    from app.businesses._components.rack_dispatch.models import RackAction, RackJob
    from app.integrations.wms_transport.dispatch import TransportDispatch
    from app.models import ReliableMessage, TransportTask
    from app.reliable.store import cleanup_terminal

    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    inbound = await ready_rack_put(x)
    await placed(x, inbound)
    await x.drain()
    x.replies["ctu03_storage"] = True
    x.decisions["put_target_decide"] = {
        "result": "DEPART_REQUIRED",
        "location_code": x.point,
        "current_rack": {"rack_id": "RACK-A"},
    }
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    await outlet(x, reel_id=str(uuid4()))
    for _ in range(8):
        await x.advance()
        await x.dispatch()
        if len(x.transports) == 2:
            break
    _, departure = x.transports
    assert departure["data"]["rcs_template_id"] == "CTU03"
    x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 60_000}
    await x.drain()
    (closed,) = await x.rack_jobs()
    assert not closed.pending and closed.closure_reason == "RACK_ALREADY_IN_STORAGE"
    async with callback_store.begin() as session:
        identity = await session.scalar(
            select(ReliableMessage.id).where(
                ReliableMessage.operation_id == departure["operation_id"]
            )
        )
        assert (
            await cleanup_terminal(
                session,
                now=datetime.now(UTC) + timedelta(days=91),
                retention_days=90,
                candidate_ids=(identity,),
            )
            == 1
        )

    if x.dialect == "postgresql":
        # Seed the closed predecessor as a retained Roller row from the prior point assignment.
        async with callback_store.begin() as session:
            job = await session.get(RackJob, closed.id)
            old = WorkLine(
                line_code=f"OLD-{uuid4().hex}",
                line_name="Old Roller line",
                business_code="smt_manual",
                is_enabled=False,
            )
            session.add(old)
            await session.flush()
            rows = list(
                (
                    await session.scalars(
                        select(RackAction)
                        .where(RackAction.job_id == job.id)
                        .order_by(RackAction.ordinal)
                    )
                ).all()
            )
            actions = [
                {c.key: getattr(action, c.key) for c in RackAction.__table__.columns}
                for action in rows
            ]
            origin = {c.key: getattr(job, c.key) for c in RackJob.__table__.columns}
            for action in reversed(actions):
                await session.execute(delete(RackAction).where(RackAction.id == action["id"]))
            await session.execute(delete(RackJob).where(RackJob.id == closed.id))
            origin.update(
                owner_domain="smt_manual", work_line_id=old.id, source_kind="ROLLER_ORDINARY"
            )
            await session.execute(insert(RackJob).values(origin))
            for action in actions:
                action.update(owner_domain="smt_manual", work_line_id=old.id)
                await session.execute(insert(RackAction).values(action))
            await session.execute(
                update(TransportTask)
                .where(
                    TransportTask.transport_task_id.in_(
                        [
                            action["transport_task_id"]
                            for action in actions
                            if action["transport_task_id"]
                        ]
                    )
                )
                .values(owner_domain="smt_manual")
            )
    x.decisions["put_target_decide"] = rack_slot(x, rack="RACK-B", face="B") | {
        "target_preparation": "REPLACE",
    }
    await outlet(x, reel_id=str(uuid4()))
    await x.advance()
    await x.dispatch()
    for _ in range(4):
        await x.advance()
        async with callback_store() as session:
            queued = (
                await session.execute(
                    select(RackAction.submit_operation_id, ReliableMessage.status)
                    .join(RackJob, RackJob.id == RackAction.job_id)
                    .join(ReliableMessage, ReliableMessage.id == RackAction.submit_message_id)
                    .where(RackJob.rack_id == "RACK-B", RackAction.kind == "ENTER")
                )
            ).one_or_none()
        if queued:
            break
    assert queued and queued.status == "PENDING"
    assert len(x.transports) == 2

    async def late_result():
        await x.transport_result(
            departure,
            status="FAILED",
            failure_code="RCS_EXECUTION_FAILED",
            final_position={"kind": "RACK_POSITION", "location_code": x.point},
        )
        async with callback_store() as session:
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == departure["data"]["transport_task_id"]
                )
            )
            assert task.pending_outcome or task.applied_outcome

    from app.businesses._components.rack_dispatch.service import RackDispatch
    from app.reliable.lifecycle import EvidencePersistenceError

    operation = queued.submit_operation_id
    waiting, release, probe = pause_after_partial_body(
        monkeypatch, identity=operation, peer=x.ecs_origin, after_probe=True
    )
    fault_injected = False
    pg_pid = None
    authorize = RackDispatch.authorize_queued_entry

    async def capture(dispatch, session, *, task):
        nonlocal pg_pid
        reason = await authorize(dispatch, session, task=task)
        if task.operation_id == operation and not fault_injected:
            assert reason is None
            pg_pid = await session.scalar(text("select pg_backend_pid()"))
        return reason

    monkeypatch.setattr(RackDispatch, "authorize_queued_entry", capture)
    async with database_relay(callback_store) as (relay_factory, forward):
        open_guard = TransportDispatch.http_guard

        @asynccontextmanager
        async def route(service, factory, claim):
            applies = claim.message.operation_id == operation
            async with open_guard(service, relay_factory if applies else factory, claim) as auth:
                if not applies:
                    yield auth
                    return

                async def check():
                    await probe(auth[1])

                yield auth[0], check, auth[2], auth[3]

        monkeypatch.setattr(TransportDispatch, "http_guard", route)

        async def fail_after_gate():
            nonlocal fault_injected
            async with asyncio.timeout(5):
                await waiting.wait()
                forward.clear()
                try:
                    if fault != "short_only":
                        async with callback_store.begin() as session:
                            assert await session.scalar(
                                text("select pg_terminate_backend(:pid, 5000)"), {"pid": pg_pid}
                            )
                    if fault == "already_reported":
                        await late_result()
                    fault_injected = True
                    release.set()
                    await asyncio.sleep(0.15)
                finally:
                    forward.set()

        x.restart()
        await x.restart_sender()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(fail_after_gate())
            with suppress(EvidencePersistenceError):
                await x.dispatch()
    assert fault_injected
    assert not any(req["operation_id"] == operation for req in x.transports)
    async with callback_store() as session:
        before = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.operation_id == operation)
        )
        assert before.status in ("STALLED", "SENDING"), before.status
    monkeypatch.setattr(TransportDispatch, "http_guard", open_guard)
    await x.restart_sender()
    for _ in range(3):
        x.clock.advance(40)
        await x.dispatch()
    resent = [req for req in x.transports if req["operation_id"] == operation]
    if fault == "delayed_result":
        await late_result()
    assert not resent, (before.status, before.delivery_uncertain, before.last_failure_kind)
