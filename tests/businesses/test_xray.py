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
    assert {b.task_type for b in bindings if b.annex_id.startswith("xray.")} == {
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
                    "data": {"eventId": event_id, "reelId": reel_id or reel, **data},
                },
            )
            assert response.status_code == status, response.text
            return event_id

        async def entry(**changes):
            return await event(
                "SCAN_COMPLETED",
                **{
                    "scanStage": "ENTRY",
                    "scanStatus": "OK",
                    "barcode": original,
                    "templateId": TEMPLATE,
                    **changes,
                },
            )

        async def result(cmd, *, status=200, failure=False, **data):
            body = {
                "command_code": cmd["command_code"],
                "device_code": cmd["device_code"],
                "result": "FAILED" if failure else "SUCCESS",
                "finish_time": stamp + 1,
                "data": {"reelId": cmd["params"]["reelId"], **data},
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
    assert first["params"]["sourceEventId"] == entry_id
    if counted:
        await x.event(
            "XRAY_COUNT_COMPLETED", barcode=x.barcode, templateId=TEMPLATE, countStatus="OK", qty=0
        )
        await x.drain()
        assert x.requests[1]["data"]["actual_qty"] == 0
        assert x.commands[1]["params"]["label"] == {"variables": LABEL}
        await x.result(first, decision="COUNT_REQUIRED", qty=0)
        await x.event(
            "SCAN_COMPLETED",
            scanStage="VERIFY",
            scanStatus="OK",
            barcode=x.barcode,
            new_barcode="actual-new",
        )
        await x.drain()
        await x.result(x.commands[1], decision="OK", labelStatus="APPLIED")
    mode = "COUNTED" if counted else "BYPASS"
    outlet = x.commands[-1]
    arrived = dict(barcode=x.barcode, mode=mode, readyForPick=True)
    if counted:
        arrived["new_barcode"] = "actual-new"
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
        await x.result(put, actualQty=1, position="TARGET", actualTarget=put["params"]["target"])
        await x.drain()
    assert len(x.facts) == 1
    assert x.facts[0]["data"]["to_locator"] == {"type": "TRANSIT_BIN"}
    assert not x.mock_facts


async def test_wait_ng_request_stops_normal_decision_and_reports_once(xray):
    x = xray
    x.decisions["pkg_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    entry_id = await x.entry()
    await x.drain()
    assert not x.commands
    await x.event(
        "XRAY_NG_REQUESTED",
        sourceEventId=entry_id,
        reasonCode="NO_CAPACITY",
        reasonMessage="cannot wait",
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
        sourceEventId=entry_id,
        reasonCode="NO_CAPACITY",
        reasonMessage="cannot wait",
    )
    x.clock.advance(301)
    await x.drain()
    assert len(x.commands) == (2 if rejected else 1)
    if rejected:
        ng = x.commands[-1]
        assert ng["params"]["decision"] == "NG"
        assert ng["params"]["sourceEventId"] == request_id
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
            "XRAY_COUNT_COMPLETED", barcode=x.barcode, templateId=TEMPLATE, countStatus="OK", qty=1
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
    await x.entry(scanStatus="NG", reasonCode="NO_BARCODE", reasonMessage="unreadable")
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
        templateId=TEMPLATE,
        countStatus="NG",
        reasonCode="COUNT_FAILED",
        reasonMessage="measurement failed",
    )
    await x.event("NG_DISCHARGED", stage="COUNT", barcode=x.barcode, reason="count failed")
    await x.drain()
    assert len(x.facts) == 1
    await x.result(cmd, failure=True, failureStage="COUNT")
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
    await x.event("MATERIAL_ARRIVED", barcode=x.barcode, mode="BYPASS", readyForPick=True)
    await x.drain()
    cmd = x.commands[-1]
    target = {**cmd["params"]["target"], "bin_id": "WRONG-BIN"}
    await x.result(cmd, actualQty=1, position="TARGET", actualTarget=target)
    await x.drain()
    assert len(x.commands) == 2
    assert not x.facts


async def test_strict_event_and_result_shapes_reject_coercion_and_extra_fields(xray):
    x = xray
    await x.entry(status=400, barcode=None)
    await x.entry(status=400, extra="unexpected")
    x.decisions["pkg_decide"] = {"result": "COUNT_REQUIRED"}
    await x.entry()
    await x.drain()
    (cmd,) = x.commands
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
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, templateId=TEMPLATE, countStatus="OK", qty=0
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
    assert len({c["params"]["reelId"] for c in x.commands}) == 2
    for c in x.commands[:]:
        await x.event(
            "MATERIAL_ARRIVED",
            reel_id=c["params"]["reelId"],
            barcode=x.barcode,
            mode="BYPASS",
            readyForPick=True,
        )
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.commands) == 3
    put = x.commands[-1]
    await x.result(put, actualQty=1, position="TARGET", actualTarget=put["params"]["target"])
    await x.drain()
    assert len(x.facts) == 1
    assert len(x.commands) == 4
    assert x.commands[-1]["params"]["reelId"] != put["params"]["reelId"]


async def outlet(x, *, reel_id=None):
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    await x.entry(reel_id=reel_id)
    await x.drain()
    cmd = next(c for c in reversed(x.commands) if c["task_type"] == "APPLY_PKG_DECISION")
    await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
    return await x.event(
        "MATERIAL_ARRIVED", reel_id=reel_id, barcode=x.barcode, mode="BYPASS", readyForPick=True
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
            templateId=TEMPLATE,
            countStatus="OK",
            qty=index,
        )
        await x.result(first, decision="COUNT_REQUIRED", qty=index)
        await x.drain()
        label_cmd = x.commands[-1]
        label = label_cmd["params"]["label"]["variables"]
        assert label["QQTYPC"] == str(index)
        await x.result(label_cmd, decision="OK", labelStatus="APPLIED")
        await x.event(
            "SCAN_COMPLETED",
            reel_id=reel_id,
            scanStage="VERIFY",
            scanStatus="OK",
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
            readyForPick=True,
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


async def test_replenish_waits_for_original_enter_then_redecides(xray):
    x = xray
    x.decisions["put_target_decide"] = replenish(x)
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

    x.decisions["put_target_decide"] = rack_slot(x)
    await x.transport_result(enter)
    await x.drain()
    renewed = x.requests[-1]
    assert first["operation_id"] != renewed["operation_id"]
    assert first["data"] == renewed["data"]
    put = x.commands[-1]
    assert put["task_type"] == "PUT_TO_RACK"
    target = put["params"]["target"]
    assert target["location_id"] == "ECS-RACK-POINT"
    assert target["rack_id"] == "RACK-A"
    actual = {k: v for k, v in target.items() if not k.startswith("reel_")}
    await x.result(put, actualQty=1, position="TARGET", actualTarget=actual)
    await x.drain()
    assert x.facts[0]["data"]["to_locator"] == rack_slot(x)["target_locator"]


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
    await x.result(put, actualQty=1, position="TARGET", actualTarget=actual)


async def test_concurrent_reels_reuse_one_enter_across_restart(xray):
    x = xray
    if x.dialect != "postgresql":
        pytest.skip("requires PostgreSQL serializable transactions")
    x.decisions["put_target_decide"] = replenish(x)
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    reel_ids = [str(uuid4()), str(uuid4())]
    for reel in reel_ids:
        await x.entry(reel_id=reel)
    await x.drain()
    event_ids = set()
    for cmd in x.commands[:]:
        await x.result(cmd, decision="BYPASS_COUNT", mode="BYPASS")
        event_ids.add(
            await x.event(
                "MATERIAL_ARRIVED",
                reel_id=cmd["params"]["reelId"],
                barcode=x.barcode,
                mode="BYPASS",
                readyForPick=True,
            )
        )
    await asyncio.gather(*(x.advance_reel(reel) for reel in reel_ids))
    assert await x.dispatch() == 2
    await asyncio.gather(*(x.advance_reel(reel) for reel in reel_ids))
    await x.drain()
    (enter,) = x.transports
    first = {r["data"]["source_event_id"]: r for r in target_requests(x)}
    assert set(first) == event_ids
    x.restart()
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert x.transports == [enter]
    assert len(target_requests(x)) == 2

    x.decisions["put_target_decide"] = rack_slot(x)
    await x.transport_result(enter)
    await asyncio.gather(x.advance(), x.advance())
    await x.drain()
    assert len(x.transports) == 1
    renewed = target_requests(x)[2:]
    assert len(renewed) == 2
    for r in renewed:
        old = first[r["data"]["source_event_id"]]
        assert r["data"] == old["data"]
        assert r["operation_id"] != old["operation_id"]
    (put,) = [c for c in x.commands if c["task_type"] == "PUT_TO_RACK"]
    await placed(x, put)
    await x.drain()
    assert len([c for c in x.commands if c["task_type"] == "PUT_TO_RACK"]) == 2


@pytest.mark.parametrize("mode", ["ROTATE_REQUIRED", "DEPART_REQUIRED", "REPLACE_REQUIRED"])
async def test_rack_move_waits_for_original_put_and_resumes(xray, mode):
    x = xray
    first_put = await ready_rack_put(x)
    plan = {"result": mode, "location_code": x.point, "current_rack": {"rack_id": "RACK-A"}}
    if mode == "ROTATE_REQUIRED":
        plan["target_face"] = "B"
    elif mode == "REPLACE_REQUIRED":
        plan["incoming_rack"] = {"rack_id": "RACK-B", "target_face": "B"}
    x.decisions["put_target_decide"] = plan
    x.decisions["departure_decide"] = {
        "result": "READY",
        "rack_destination": {"type": "ZONE", "location_code": "RETURN-STOCK"},
    }
    source = await outlet(x, reel_id=str(uuid4()))
    await x.drain()
    original = target_requests(x)[-1]
    assert original["data"]["source_event_id"] == source
    x.clock.advance(301)
    x.restart()
    await x.drain()
    assert len(x.transports) == 1
    assert not any(r["operation"] == "rack.departure_decide@v1" for r in x.requests)

    if mode == "DEPART_REQUIRED":
        x.decisions["put_target_decide"] = {"result": "WAIT", "retry_after_ms": 1000}
    await placed(x, first_put)
    await x.drain()
    if mode == "ROTATE_REQUIRED":
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
        if mode == "DEPART_REQUIRED":
            assert not remaining
            assert target_requests(x)[-1]["operation_id"] != original["operation_id"]
            assert target_requests(x)[-1]["data"] == original["data"]
            await x.transport_result(
                depart, final_position={"kind": "RACK_POSITION", "location_code": "STOCK-SLOT"}
            )
            x.restart()
            await x.drain()
            assert len(x.transports) == 2
            return
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

    rack = "RACK-B" if mode == "REPLACE_REQUIRED" else "RACK-A"
    x.decisions["put_target_decide"] = rack_slot(x, rack=rack, face="B")
    await x.transport_result(move)
    await x.drain()
    assert target_requests(x)[-1]["operation_id"] != original["operation_id"]
    assert target_requests(x)[-1]["data"] == original["data"]
    final_put = x.commands[-1]
    assert final_put["task_type"] == "PUT_TO_RACK"
    assert final_put["params"]["target"]["rack_id"] == rack
    assert final_put["params"]["target"]["rack_face"] == "B"


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
    for identity in wait_ids:
        await x.entry(reel_id=identity)
    for _ in range(4):
        identity = str(uuid4())
        event = await x.entry(reel_id=identity)
        await x.entry(reel_id=identity, event_id=event, barcode="CONFLICTING")
    await x.drain()
    old_requests = list(x.requests)
    assert {r["data"]["reel_id"] for r in old_requests} == set(wait_ids)
    assert not x.commands

    notices.clear()
    ready_ids = [str(uuid4()) for _ in range(7)]
    x.decisions["pkg_decide"] = {"result": "BYPASS_COUNT"}
    for identity in ready_ids:
        await x.entry(reel_id=identity)
    assert notices == ["xray"] * len(ready_ids)
    # The worker must cross three pages without opening transactions for sleeping reels.
    assert await tick(notices[0]) == len(ready_ids)
    await asyncio.gather(tick(notices[0]), tick())
    while await x.dispatch():
        pass
    assert x.requests[: len(old_requests)] == old_requests
    assert sorted(r["data"]["reel_id"] for r in x.requests[len(old_requests) :]) == sorted(
        ready_ids
    )
    await asyncio.gather(tick(notices[-1]), tick())
    while await x.dispatch():
        pass
    assert sorted(c["params"]["reelId"] for c in x.commands) == sorted(ready_ids)
    await tick()
    assert await x.dispatch() == 0

    # A new original device fact can make a WAIT reel eligible before its WMS deadline.
    prior = next(r for r in old_requests if r["data"]["reel_id"] == wait_ids[0])
    request_id = await x.event(
        "XRAY_NG_REQUESTED",
        reel_id=wait_ids[0],
        sourceEventId=prior["data"]["source_event_id"],
        reasonCode="NO_CAPACITY",
        reasonMessage="cannot wait",
    )
    await tick(notices[-1])
    await x.dispatch()
    ng = x.commands[-1]
    assert ng["params"]["decision"] == "NG"
    assert ng["params"]["sourceEventId"] == request_id
    assert len(x.requests) == len(old_requests) + len(ready_ids)

    # The periodic invocation discovers due waits even without a fresh callback.
    notices.clear()
    x.clock.advance(60)
    await tick()
    while await x.dispatch():
        pass
    renewed = x.requests[len(old_requests) + len(ready_ids) :]
    assert {r["data"]["reel_id"] for r in renewed} == set(wait_ids[1:])
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
        "XRAY_COUNT_COMPLETED", barcode=x.barcode, templateId=TEMPLATE, countStatus="OK", qty=1
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


async def test_departure_wait_reuses_original_until_due(xray):
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
