"""Run the formal X-Ray entry points and Sender against disposable peers."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

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
from app.integrations.wms_request import ADAPTERS
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
    bindings, _ = catalog.validate_business_contracts()
    assert "xray" in {item.code for item in catalog.definitions()}
    xray_bindings = [binding for binding in bindings if binding.annex_id.startswith("xray.")]
    assert {binding.annex_version for binding in xray_bindings} == {"2"}
    assert {binding.task_type for binding in xray_bindings} == {
        "APPLY_PKG_DECISION",
        "APPLY_COUNT_DECISION",
        "APPLY_VERIFY_DECISION",
        "PUT_TO_BIN",
        "PUT_TO_RACK",
    }


@pytest.fixture
async def xray(callback_store, monkeypatch):
    factory = callback_store
    decisions, requests, commands, facts = {}, [], [], []
    mock_facts = []
    transports = []
    replies = {}
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
        return {
            "operation_id": body["operation_id"],
            "code": "RECORDED",
            "timestamp": 2,
            "data": {},
        }

    @peer.post("/api/v1/wes/transport-requests")
    async def transport(req: Request):
        body = await req.json()
        transports.append(body)
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
                    "templates": {"ENTER": "F01", "ROTATE": "CK04", "DEPART": "F01"},
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
        lifecycle = {"ecs-command": EcsLifecycle(ecs)}
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
        store = MessageStore(factory, lifecycles=lifecycle, records=history, clock=clock)
        sender = Sender(
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
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        reel = str(uuid4())
        original = "001-original"
        stamp = 1793413814500

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
            body = {
                "command_code": cmd["command_code"],
                "device_code": cmd["device_code"],
                "result": "FAILED" if failure else "SUCCESS",
                "finish_time": stamp + 1,
                "data": {"reel_id": cmd["params"]["reel_id"], **data},
            }
            if failure:
                body["error_detail"] = {"code": "FAILED", "msg": "device failure"}
            response = await client.post("/api/v1/callback/result", json=body)
            assert response.status_code == status, response.text

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
            store.on_result = notifications.wms_result_committed

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

        try:
            yield SimpleNamespace(
                decisions=decisions,
                requests=requests,
                commands=commands,
                facts=facts,
                mock_facts=mock_facts,
                replies=replies,
                transports=transports,
                transport_result=transport_result,
                point=point,
                event=event,
                entry=entry,
                result=result,
                drain=drain,
                advance=advance,
                wakeups=wakeups,
                advance_reel=advance_reel,
                change_binding=change_binding,
                dispatch=dispatch,
                restart=restart,
                use_mock=use_mock,
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
        pkg_decide={"result": "COUNT_REQUIRED" if counted else "BYPASS_COUNT"},
        count_decide={"result": "OK", "label_variables": LABEL},
        verify_decide={"result": "OK"},
        put_target_decide={"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}},
    )
    entry_id = await x.entry()
    await x.drain()
    first = x.commands[0]
    assert first["params"]["source_event_id"] == entry_id
    if counted:
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
        await x.result(first, decision="COUNT_REQUIRED", qty=0)
        await x.event(
            "SCAN_COMPLETED",
            scan_stage="VERIFY",
            scan_status="OK",
            barcode=x.barcode,
            new_barcode=LABEL["SPKG"],
        )
        await x.drain()
        assert x.requests[2]["data"]["expected_spkg"] == LABEL["SPKG"]
        await x.result(x.commands[1], decision="OK", label_status="APPLIED")
    mode = "COUNTED" if counted else "BYPASS"
    outlet = x.commands[-1]
    arrived = dict(barcode=x.barcode, mode=mode, ready_for_pick=True)
    if counted:
        arrived["new_barcode"] = LABEL["SPKG"]
    await x.result(outlet, decision="OK" if counted else "BYPASS_COUNT", mode=mode)
    await x.event("MATERIAL_ARRIVED", **arrived)
    await x.event("MATERIAL_ARRIVED", **arrived)
    await x.drain()
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_BIN"
    assert put["params"]["target"]["bin_location"] in "ABCD"
    assert len(x.requests) == (4 if counted else 2)
    assert len(x.commands) == (4 if counted else 2)
    assert not x.facts
    x.clock.advance(301)
    x.restart()
    await x.drain()
    assert len(x.commands) == (4 if counted else 2)
    assert x.commands[-1] == put
    assert not x.facts
    for _ in range(2):
        await x.result(put, actual_qty=1, position="TARGET", actual_target=put["params"]["target"])
        await x.drain()
    assert len(x.facts) == 1
    assert x.facts[0]["data"]["to_locator"] == {"type": "TRANSIT_BIN"}
    assert not x.mock_facts
    assert all(
        not {"reel_id", "source_event_id"} & request["data"].keys()
        for request in (*x.requests, *x.facts)
    )
    assert x.facts[0]["data"]["target_operation_id"] == x.requests[-1]["operation_id"]


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
        pkg_decide={"result": "COUNT_REQUIRED"},
        count_decide={"result": "OK", "label_variables": LABEL},
        verify_decide={"result": "OK"},
    )
    await x.entry()
    await x.drain()
    await x.event(
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, template_id=TEMPLATE, count_status="OK", qty=0
    )
    await x.drain()
    await x.event(
        "SCAN_COMPLETED",
        scan_stage="VERIFY",
        scan_status="OK",
        barcode=x.barcode,
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
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED"}
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
        pkg_decide={"result": "COUNT_REQUIRED"},
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
    await x.event("NG_DISCHARGED", stage="ENTRY", reason="scanner failed")
    await x.drain()
    assert not x.facts
    await x.entry(scan_status="NG", reason_code="NO_BARCODE", reason_message="unreadable")
    await x.drain()
    assert not x.requests
    assert not x.commands
    assert len(x.facts) == 1
    assert x.facts[0]["data"]["reason_code"] == "DEVICE_NG_DISCHARGED"


async def test_technical_count_discharge_does_not_wait_for_failed_result(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED"}
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
    await x.result(cmd, failure=True, failure_stage="COUNT")
    await x.drain()
    assert len(x.facts) == len(x.commands) == len(x.requests) == 1


async def test_wrong_bin_result_is_retained_without_put_report(xray):
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
    target = {**cmd["params"]["target"], "bin_id": "WRONG-BIN"}
    await x.result(cmd, actual_qty=1, position="TARGET", actual_target=target)
    await x.drain()
    assert len(x.commands) == 2
    assert not x.facts


async def test_strict_event_and_result_shapes_reject_coercion_and_extra_fields(xray):
    x = xray
    await x.entry(status=400, barcode=None)
    await x.entry(status=400, eventId=str(uuid4()))
    await x.entry(status=400, extra="unexpected")
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED"}
    await x.entry()
    await x.drain()
    (cmd,) = x.commands
    await x.result(cmd, status=400, decision="COUNT_REQUIRED", qty=0, reelId=str(uuid4()))
    await x.result(cmd, status=400, decision="COUNT_REQUIRED", qty=True)
    await x.result(cmd, status=400, decision="COUNT_REQUIRED", qty=0, extra="unexpected")
    await x.result(cmd, decision="COUNT_REQUIRED", qty=0)
    await x.drain()
    assert len(x.requests) == 1


async def test_invalid_wms_decision_retries_original_operation(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED", "extra": "invalid"}
    await x.entry()
    await x.drain()
    assert len(x.requests) == 1
    assert not x.commands
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED"}
    x.clock.advance(60)
    await x.drain()
    assert len(x.requests) == 2
    assert x.requests[0] == x.requests[1]
    assert x.commands[0]["params"]["decision"] == "COUNT_REQUIRED"


async def test_event_cannot_override_terminal_nonacceptance(xray):
    x = xray
    x.decisions.update(
        pkg_decide={"result": "COUNT_REQUIRED"},
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


async def test_two_reels_share_one_unsettled_arm_command(xray):
    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    x.decisions.update(
        pkg_decide={"result": "BYPASS_COUNT"},
        put_target_decide={"result": "ACCEPT", "target_locator": {"type": "TRANSIT_BIN"}},
    )
    await x.entry()
    await x.entry(reel_id=str(uuid4()))
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.commands) == 2
    assert len({c["params"]["reel_id"] for c in x.commands}) == 2
    for c in x.commands[:]:
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=c["params"]["reel_id"],
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
    assert x.commands[-1]["params"]["reel_id"] != put["params"]["reel_id"]


async def outlet(x, *, reel_id=None):
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    await x.entry(reel_id=reel_id)
    await x.drain()
    cmd = next(c for c in reversed(x.commands) if c["task_type"] == "APPLY_PKG_DECISION")
    await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
    return await x.event(
        "MATERIAL_ARRIVED", reel_id=reel_id, barcode=x.barcode, mode="BYPASS", ready_for_pick=True
    )


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
            new_barcode=label["SPKG"],
        )
        await x.drain()
        await x.result(x.commands[-1], decision="OK", mode="COUNTED")
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=reel_id,
            barcode=barcode,
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
    assert x.facts[0]["data"]["target_operation_id"] == first["operation_id"]


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
    for reel, column in materials.items():
        await x.entry(reel_id=reel, barcode=str(column))
    await x.drain()
    for cmd in x.commands[:]:
        await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=cmd["params"]["reel_id"],
            barcode=str(materials[cmd["params"]["reel_id"]]),
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
    assert sorted(c["params"]["reel_id"] for c in x.commands) == sorted(ready_ids)
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
        pkg_decide={"result": "COUNT_REQUIRED"},
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
