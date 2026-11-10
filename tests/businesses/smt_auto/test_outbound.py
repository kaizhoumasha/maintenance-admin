"""Normal inbound consumes actual ECS results and actual WMS outbox responses."""

import importlib
import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import (
    AutoBinWork,
    AutoOutboundDecisionRound,
    AutoOutboundPlate,
    AutoOutboundSourceMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.businesses.smt_auto.rack_ports import AutoRackPorts, RackAuthorization
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.ecs_callback.receiver import receive_ecs_event
from app.integrations.ecs_command import compile_commands
from app.integrations.ecs_command.result import receive_ecs_result
from app.integrations.ecs_command.service import CommandOwner
from app.integrations.wms_request import ADAPTERS, FACTS_PROTOCOL
from app.models import Device, DeviceRoute, IntegrationExchange, WorkLine, WorkLinePosition
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.services.device_bindings import resolve_binding
from tests.businesses.rack_dispatch import apply_outcome, origin
from tests.helpers import read_history_body


@pytest.fixture(params=[("ARM02", "ARM01"), ("ARM04", "ARM03")])
async def outbound_host(s5_roller, request, monkeypatch):
    host = s5_roller
    rack_arm, bin_arm = request.param[:2]
    for name in (
        "outbound_bin_material_decide_v1",
        "outbound_rackslot_material_decide_v1",
        "outbound_material_movement_report_v1",
        "outbound_source_empty_decide_v1",
    ):
        module = f"app.businesses.smt_auto.request.{name}"
        if module in sys.modules:
            importlib.reload(sys.modules[module])
        else:
            importlib.import_module(module)
    from app.businesses.smt_auto.ecs_command import PickAndPut

    if "app.businesses.smt_auto.inbound" in sys.modules:
        importlib.reload(sys.modules["app.businesses.smt_auto.inbound"])
    from app.businesses.smt_auto.inbound import PhysicalSettings

    if "app.businesses.smt_auto.outbound" in sys.modules:
        importlib.reload(sys.modules["app.businesses.smt_auto.outbound"])
    from app.businesses.smt_auto.outbound import Outbound

    ecs = host.roller.capabilities.ecs
    from app.businesses import catalog
    from app.businesses.smt_auto.definition import DEFINITION
    from app.integrations.wms_callback import registry as callbacks

    monkeypatch.setattr(callbacks, "_registry", {})
    from app.businesses._components.rack_dispatch.composition import COMPONENT as RACK_COMPONENT
    from app.businesses._components.sorting_task.composition import COMPONENT

    host.entry.COMPONENTS = (*host.entry.COMPONENTS, RACK_COMPONENT)
    original_modules = {
        name: dict(sys.modules[name].__dict__) if name in sys.modules else None
        for name in (
            *RACK_COMPONENT.entry_modules,
            "app.businesses.smt_auto.ecs_event.scan_completed",
        )
    }
    for module in RACK_COMPONENT.entry_modules:
        if module in sys.modules:
            importlib.reload(sys.modules[module])
        else:
            importlib.import_module(module)
    for module in COMPONENT.entry_modules:
        if ".request." not in module:
            if module in sys.modules:
                importlib.reload(sys.modules[module])
            else:
                importlib.import_module(module)

    host.entry.DEFINITION = DEFINITION
    host.entry.COMMANDS = (PickAndPut,)
    host.entry.PROGRESSION = "scan_pending"
    modules = (
        "app.businesses.smt_auto.callback.putaway_source_rack_exhausted_v1",
        "app.businesses.smt_auto.ecs_event.scan_completed",
    )
    host.entry.ENTRY_MODULES = (*host.entry.ENTRY_MODULES, *modules)
    for name in modules:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    catalog.validate_business_contracts()
    _, annexes = compile_commands(PickAndPut)
    ecs.annexes.update(annexes)
    host.requests.outbox.targets["facts"] = TargetConfig(
        "facts", "http://example.test", FACTS_PROTOCOL
    )
    async with host.factory.begin() as session:
        old_work = await session.scalar(select(AutoBinWork))
        await session.delete(old_work)
        await session.flush()
        task = await session.get(AutoTask, "t")
        task.business_type, task.plan_revision = "outbound", 1
        line = await session.get(WorkLine, 1)
        line.version = 1
        line.device_bindings = {
            "RACK_ARM": rack_arm,
            "BIN_ARM": bin_arm,
            "SCAN2": "SCAN2",
            "PLATFORM_SCANNER": "SCANNER_001",
        }
        line.position_bindings = {"TRANSFER_RACK": "P", "RETURN_RACK": "Q"}
        session.add(WorkLinePosition(work_line_id=1, position_code="P", capacity=None))
        session.add(WorkLinePosition(work_line_id=1, position_code="Q", capacity=None))
        for index, code in enumerate((rack_arm, bin_arm, "SCAN2", "SCANNER_001"), start=1):
            route_id = f"r{index}"
            session.add(DeviceRoute(id=route_id, device_code=code, ecs_origin="http://ecs.test"))
            await session.flush()
            session.add(
                Device(
                    id=index,
                    device_code=code,
                    display_name=code,
                    work_line_id=1,
                    current_route_id=route_id,
                    version=1,
                )
            )
        plan = AutoTaskInput(
            task_id="t",
            operation="sorting.task.plan_delta@v1",
            operation_id="plan",
            request_timestamp=1,
            first_received_at_ms=1,
            effective_digest="d",
            contract_version="v1",
            disposition="APPLIED",
            applied_plan_revision=1,
            effective_request={"target_rack": {"rack_id": "target", "rack_face": "A"}},
        )
        session.add(plan)
        await session.flush()
        binding = await resolve_binding(session, rack_arm)
        auth = RackAuthorization("FIRST_TARGET", plan.id)
        job_origin = replace(
            origin("AUTO_OUTBOUND_TARGET"),
            work_line_id=1,
            task_id="t",
            source_ref=auth.source_ref,
            initial_face="A",
            rack_id="target",
            point="P",
            resources=asdict(binding),
        )
        ports = AutoRackPorts(host.dispatch)
        job = await ports.register(
            session, job_origin, authorization=auth, original_binding=binding
        )
        assert job
        action = await ports.prepare(
            session,
            job_id=job,
            kind="ENTER",
            authorization=auth,
            face="A",
            original_binding=binding,
        )
        progress = await host.dispatch.progress(session, job_id=job)
        await apply_outcome(
            session,
            progress.actions[-1],
            "SUCCEEDED",
            point="P",
            rack_id="target",
            arrival_face="A",
        )
        await ports.advance(session, job_id=job, now_ms=10)
        assert action
    if len(request.param) < 3 or request.param[2] != "NO_BIN":
        work = await host.scan()
        host.work_id = work.id
    host.outbound = Outbound(
        host.requests,
        ecs,
        host.dispatch,
        host.roller,
        settings=PhysicalSettings(1, 1, 30001, 30002, (("P", "STATION_A"), ("Q", "STATION_B"))),
    )
    host.ecs = ecs

    pending_claims = []

    async def settle(operation, data, code="DECIDED"):
        store = MessageStore(host.factory, records=history_records)
        pending_claims.extend(await store.claim(100, host.requests.outbox.targets))
        claims = pending_claims
        matching = [c for c in claims if c.message.operation == operation]
        assert len(matching) == 1
        claim = matching[0]
        pending_claims.remove(claim)
        response = HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            200,
            (),
            json.dumps(
                dict(operation_id=claim.message.operation_id, code=code, timestamp=100, data=data)
            ).encode(),
        )
        protocol = "wms-facts" if code == "RECORDED" else "wms-decisions"
        verdict = ADAPTERS[protocol].classify_response(claim.message, response)
        assert await store.finish(
            claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
        )

    async def result(code, *, scan=False, status="SUCCESS", empty=False, legacy_scan=False):
        store = MessageStore(
            host.factory, lifecycles=ecs.outbox.lifecycles, records=history_records
        )
        for claim in await store.claim(100, ecs.outbox.targets):
            assert await store.before_send(claim, ecs.outbox.targets["ecs"])
        async with host.factory.begin() as session:
            view = await ecs.get_command(session, owner=CommandOwner("smt_auto"), command_code=code)
            data = dict(actual_qty=1, pick_and_put_result="PUT_FINISHED")
            if empty:
                data = dict(
                    actual_qty=0, pick_and_put_result="PICK_EMPTY" if empty is True else empty
                )
            separate_scan = scan and view.device_code == bin_arm and not legacy_scan
            if legacy_scan or (scan and not separate_scan):
                data.update(barcode="原始料盘," * 60, scanned_at=88)
            body = dict(
                command_code=code,
                device_code=view.device_code,
                result=status,
                finish_time=99,
                data=data,
            )
            if status == "FAILED":
                body["error_detail"] = dict(code="STOPPED", msg="stopped")
            response = await receive_ecs_result(session, json.dumps(body).encode(), ecs)
        if separate_scan:
            assert (await platform_scan(code)).status_code == 200
        return response

    async def platform_scan(code, *, barcode="原始料盘," * 60, timestamp=88, device="SCANNER_001"):
        from app.businesses.runtime import BusinessRuntime

        body = {
            "device_code": device,
            "event_type": "SCAN_COMPLETED",
            "timestamp": timestamp,
            "data": {
                "location": "SCAN_PLATFORM",
                "barcode": barcode,
                "previous_command_code": code,
            },
        }
        runtime = BusinessRuntime({}, {"smt_auto": SimpleNamespace(outbound=host.outbound)})
        async with host.factory.begin() as session:
            return await receive_ecs_event(
                session,
                json.dumps(body).encode(),
                resolve_binding=resolve_binding,
                resolve_input_target=catalog.input_target,
                runtime=runtime,
            )

    host.platform_scan = platform_scan
    host.settle, host.arm_result = settle, result
    yield host
    for name, namespace in original_modules.items():
        if namespace is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name].__dict__.clear()
            sys.modules[name].__dict__.update(namespace)


@pytest.mark.parametrize(
    "wait,disabled,cancel",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
@pytest.mark.parametrize("legacy_diameter", [False, True])
async def test_original_bin_pick_put_report_survives_wait_and_disable(
    outbound_host, wait, disabled, cancel, legacy_diameter, monkeypatch
):
    from app.businesses.smt_auto.ecs_command import PickAndPut

    builder = PickAndPut.from_millimeters
    if legacy_diameter:
        # Create frozen commands using the builder used before diameter conversion.
        monkeypatch.setattr(
            PickAndPut, "from_millimeters", classmethod(lambda cls, **params: cls(**params))
        )
    host = outbound_host
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20
        )
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C1",
            cell_index=1,
            bin_type=8,
            reel_layer=2,
            reel_thickness=15.3,
            reel_diameter=179,
        ),
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoOutboundPlate))
        plate_id = plate.id
        if cancel:
            (await session.get(AutoTask, "t")).status = "CANCELLED"
        if disabled:
            line = await session.get(WorkLine, 1)
            bindings = line.device_bindings
            line.device_bindings = {k: v for k, v in bindings.items() if k != "PLATFORM_SCANNER"}
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=35)
            assert plate.pick_command_code is None
            line.device_bindings = bindings
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=40)
        assert plate.pick_command_code
        pick_code = plate.pick_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == pick_code,
            )
        )
        assert (
            json.loads(await read_history_body(session, (command).request_record_id))["params"][
                "source"
            ]["reel_totalthickness"]
            == 30.6
        )
        raw = json.loads(await read_history_body(session, command.request_record_id))
        assert raw["params"]["source"]["bin_type"] == "8"
        assert raw["params"]["source"]["reel_diameter"] == (179 if legacy_diameter else 7)
        assert plate.source_params["reel_diameter"] == 179
        if legacy_diameter:
            snapshot = plate.source_arrival_snapshot
            command = {
                k: v for k, v in snapshot["pick_command"].items() if k != "platform_scanner_id"
            }
            plate.source_arrival_snapshot = {**snapshot, "pick_command": command}
    if legacy_diameter and disabled:
        # A pending command predating the split can still use its original Result's scan.
        assert (await host.arm_result(pick_code, legacy_scan=True)).status_code == 200
    else:
        if wait:
            assert (await host.platform_scan(pick_code)).status_code == 200
        else:
            assert (
                await host.arm_result(pick_code, legacy_scan=not legacy_diameter)
            ).status_code == 200
            wrong_pick = await host.platform_scan("wrong-pick-command")
            assert wrong_pick.diagnostic[0]["reason_code"] == "PLATFORM_SCAN_PICK_NOT_FOUND"
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=45)
            assert not await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE"
                )
            )
        if wait:
            assert (await host.arm_result(pick_code)).status_code == 200
        else:
            assert (await host.platform_scan(pick_code)).status_code == 200
        assert (await host.platform_scan(pick_code)).outcome == "duplicate"
        conflict = await host.platform_scan(pick_code, barcode="different")
        assert conflict.diagnostic[0]["reason_code"] == "PLATFORM_SCAN_CONFLICT"
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        rounds = (
            await session.scalars(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE"
                )
            )
        ).all()
        assert len(rounds) == 1
        assert rounds[0].request_data["barcode"] == "原始料盘," * 60
        assert rounds[0].request_data["scanned_at"] == 88
    if wait:
        await host.settle(
            "outbound.bin.material.decide@v1", dict(result="WAIT", retry_after_ms=100)
        )
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=159)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AutoOutboundDecisionRound)
                    .where(AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE")
                )
                == 1
            )
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=160)
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="S1",
                rack_layer=3,
                rack_column=4,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=170)
        assert plate.put_command_code
        put_code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == put_code,
            )
        )
        raw = json.loads(await read_history_body(session, command.request_record_id))
        assert raw["params"]["target"]["reel_diameter"] == (179 if legacy_diameter else 7)
        assert raw["params"]["target"]["rack_side"] == "A"
        if legacy_diameter:
            snapshot = plate.source_arrival_snapshot
            put = snapshot["put_command"]
            legacy_target = dict(put["params"]["target"])
            legacy_target["rack_face"] = legacy_target.pop("rack_side")
            plate.source_arrival_snapshot = {
                **snapshot,
                "put_command": {**put, "params": {**put["params"], "target": legacy_target}},
            }
        if disabled:
            (await session.get(WorkLine, 1)).is_enabled = False
    monkeypatch.setattr(PickAndPut, "from_millimeters", builder)
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=180)
        assert plate.report_message_id
        assert plate.report_data["occurred_at"] == 99
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plate.target_rack_job_id)
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"
        assert plate.report_data["source_locator"] == dict(
            type="BIN_CELL", bin_code="A000000001", cell_id="C1"
        )
        assert not await host.outbound.closed(session, plate)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        original = await session.get(AutoOutboundPlate, plate_id)
        assert not await host.outbound.closed(session, original)
        snapshot = original.source_arrival_snapshot
        command = snapshot["put_command"]
        params = command["params"]
        original.source_arrival_snapshot = {
            **snapshot,
            "put_command": {
                **command,
                "params": {**params, "target": {**params["target"], "reel_diameter": 15}},
            },
        }
        await host.outbound.advance_report(session, plate_id=plate_id, now_ms=189)
        assert original.report_consumed_at_ms is None
        assert not await host.outbound.closed(session, original)
        original.source_arrival_snapshot = snapshot
        plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=190)
        assert await host.outbound.closed(session, plate)
        assert not plate.pending
        work = await session.get(AutoBinWork, host.work_id)
        assert await host.outbound.bin_closed(session, work)
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=200)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )


async def return_face(host, *, ready=True):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    racks = OutboundRacks(host.outbound, station_id="station")
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        member = AutoOutboundSourceMember(
            task_id="t",
            plan_input_id=plan.id,
            plan_revision=1,
            rack_id="return",
            rack_face="A",
            rack_array_index=0,
        )
        session.add(member)
        await session.flush()
        member_id = member.id
        await racks.advance(session, task_id="t", now_ms=20)
        assert member.rack_job_id
        progress = await host.dispatch.progress(session, job_id=member.rack_job_id)
        assert progress.origin.caller.workline_id == "1"
        await apply_outcome(
            session,
            progress.actions[-1],
            "SUCCEEDED",
            point="Q",
            rack_id="return",
            arrival_face="A",
        )
        await racks.advance(session, task_id="t", now_ms=30)
        assert member.arrived_at_ms == 30
    await host.settle(
        "outbound.return_rack.work_plan@v1",
        dict(
            result="READY",
            rack_id="return",
            rack_face="A",
            slot_id="S1",
            rack_layer=2,
            rack_column=3,
            reel_thickness=15.3,
            reel_diameter=330.2,
        )
        if ready
        else {"result": "WAIT", "retry_after_ms": 100},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=40)
        plate = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.source_member_id == member_id)
        )
        return racks, member_id, plate.id if plate else None


@pytest.mark.parametrize("next_mode", [None, "valid", "invalid", "ng"])
async def test_return_face_original_pick_and_final_target(outbound_host, next_mode):
    host = outbound_host
    racks, member_id, plate_id = await return_face(host)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        assert plate.pick_command_code
        pick_code = plate.pick_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == pick_code,
            )
        )
        raw = json.loads(await read_history_body(session, (command).request_record_id))
        assert raw["params"]["source"]["location_id"] == "STATION_B"
        assert raw["params"]["source"]["rack_layer"] == "2"
        assert raw["params"]["source"]["reel_diameter"] == 13
        assert plate.source_params["reel_diameter"] == 330.2
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
    if next_mode == "ng":
        ng = dict(type="NG_ZONE", zone_code="MATERIAL_NG_01")
        await host.settle(
            "outbound.rackslot.material.decide@v1",
            dict(
                result="REJECT",
                business_exception_code="MATERIAL_REJECTED",
                source_disposition="CLOSE",
                ng_locator=ng,
            ),
        )
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=70
            )
            put_code = plate.put_command_code
            command = await session.scalar(
                select(IntegrationExchange).where(
                    IntegrationExchange.role == "ECS_COMMAND",
                    IntegrationExchange.command_code == put_code,
                )
            )
            line = await session.get(WorkLine, 1)
            assert command.device_code == line.device_bindings["BIN_ARM"]
            assert json.loads(await read_history_body(session, (command).request_record_id))[
                "params"
            ] == dict(
                source=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
                target=dict(location_id="STATION_NG", location_type="SCAN_NG"),
            )
        assert (await host.arm_result(put_code)).status_code == 200
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=80
            )
            assert plate.report_data["to_locator"] == ng
            assert plate.report_data["occurred_at"] == 99
            assert not await host.outbound.closed(session, plate)
        await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
        async with host.factory.begin() as session:
            plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=90)
            assert await host.outbound.closed(session, plate)
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=100)
            assert not plate.pending
            member = await session.get(AutoOutboundSourceMember, member_id)
            assert not await host.outbound.face_closed(session, member)
        await host.settle("outbound.return_rack.work_plan@v1", {"result": "NO_WORK"})
        async with host.factory.begin() as session:
            await racks.advance(session, task_id="t", now_ms=110)
            member = await session.get(AutoOutboundSourceMember, member_id)
            assert await host.outbound.face_closed(session, member)
        return
    decision = dict(
        result="ACCEPT",
        target_locator=dict(
            type="RACK_SLOT",
            rack_id="target",
            rack_face="A",
            slot_id="T2",
            rack_layer=4,
            rack_column=5,
        ),
    )
    if next_mode:
        decision["next_source_action"] = dict(
            rack_id="return",
            rack_face="A" if next_mode == "valid" else "B",
            slot_id="S2",
            rack_layer=1,
            rack_column=6,
            reel_thickness=22,
            reel_diameter=13,
        )
    await host.settle("outbound.rackslot.material.decide@v1", decision)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        assert plate.put_command_code
        put_code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == put_code,
            )
        )
        target = json.loads(await read_history_body(session, (command).request_record_id))[
            "params"
        ]["target"]
        assert target["reel_thickness"] == 15.3
        assert target["reel_diameter"] == 13
        assert target["rack_layer"] == "4"
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert await host.outbound.face_source_terminal(session, member) is (next_mode is None)
        assert not await host.outbound.face_closed(session, member)
        demand, handoff = await racks.ports.facts(session, job_id=member.rack_job_id)
        assert demand.state == ("NONE" if next_mode is None else "CONTINUE")
        assert handoff.state == ("BLOCKED" if next_mode == "valid" else "CLEAR")
        assert await session.scalar(select(func.count()).select_from(AutoOutboundPlate)) == (
            2 if next_mode == "valid" else 1
        )
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=80)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=90)
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert await host.outbound.closed(session, plate)
        assert await host.outbound.face_closed(session, member) is (next_mode is None)


@pytest.fixture
async def decided_outbound(outbound_host):
    host = outbound_host
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20
        )
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C1",
            cell_index=1,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        ),
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoOutboundPlate))
        host.plate_id = plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=plate.id, now_ms=40)
        code = plate.pick_command_code
    await host.arm_result(code, scan=True)
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    return host


@pytest.mark.parametrize("has_successor", [False, True])
async def test_ready_return_rack_yields_only_to_bin_successor(decided_outbound, has_successor):
    host = decided_outbound
    _, _, rack_plate_id = await return_face(host)
    decision = dict(
        result="ACCEPT",
        target_locator=dict(
            type="RACK_SLOT",
            rack_id="target",
            rack_face="A",
            slot_id="A1",
            rack_layer=1,
            rack_column=1,
        ),
    )
    if has_successor:
        decision["next_source_action"] = dict(
            cell_id="C2",
            cell_index=2,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        )
    await host.settle("outbound.bin.material.decide@v1", decision)
    async with host.factory.begin() as session:
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = first.put_command_code
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=61
        )
        assert rack.pick_command_code is None
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=70
        )
        if has_successor:
            assert rack.pick_command_code is None
            successor = await session.scalar(
                select(AutoOutboundPlate).where(
                    AutoOutboundPlate.grant_parent_plate_id == host.plate_id
                )
            )
            await host.outbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=71)
            assert successor.pick_command_code
        else:
            assert rack.pick_command_code


@pytest.mark.parametrize("rack_ready", [False, True])
async def test_new_bin_yields_to_ready_rack_only(outbound_host, rack_ready):
    host = outbound_host
    _, _, rack_plate_id = await return_face(host, ready=rack_ready)
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=41)
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C1",
            cell_index=1,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        ),
    )
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=42)
        plate_id = await session.scalar(
            select(AutoOutboundPlate.id).where(AutoOutboundPlate.bin_work_id == host.work_id)
        )
        bin_plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=plate_id, now_ms=43
        )
        assert bool(bin_plate.pick_command_code) is not rack_ready
        if rack_ready:
            rack = await host.outbound.advance(
                session, work_line_id=1, plate_id=rack_plate_id, now_ms=44
            )
            assert rack.pick_command_code


@pytest.mark.parametrize("has_successor", [False, True])
async def test_scan2_release_follows_source_terminal_before_put_result(
    decided_outbound, has_successor
):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = decided_outbound
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    decision = dict(
        result="ACCEPT",
        target_locator=dict(
            type="RACK_SLOT",
            rack_id="target",
            rack_face="A",
            slot_id="A1",
            rack_layer=1,
            rack_column=1,
        ),
    )
    if has_successor:
        decision["next_source_action"] = dict(
            cell_id="C2",
            cell_index=2,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        )
    await host.settle("outbound.bin.material.decide@v1", decision)
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        release = dict(work_line_id=1, arrival_id=work.arrival_id)
        assert await flow.release_bin(session, **release, now_ms=55) == "WAITING"
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code and plate.report_message_id is None
        state = "WAITING" if has_successor else "RELEASING"
        assert await flow.release_bin(session, **release, now_ms=61) == state
        command = work.release_command_code
        assert bool(command) == (not has_successor)
        assert await flow.release_bin(session, **release, now_ms=62) == state
        assert work.release_command_code == command
        assert await flow.advance(session, task_id="t", now_ms=63) == "WAITING"
        put_code = plate.put_command_code
    if command:
        assert (await host.result(command)).status_code == 200
        async with host.factory.begin() as session:
            assert await flow.release_bin(session, **release, now_ms=64) == "RELEASED"
            assert await flow.advance(session, task_id="t", now_ms=65) == "WAITING"
        assert (await host.arm_result(put_code)).status_code == 200
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=110
            )
            assert plate.report_message_id


@pytest.mark.parametrize("changed", ["scanner", "rack_arm", "transfer_point", "rack", "face"])
async def test_put_reuses_current_arrival_after_unrelated_line_change(decided_outbound, changed):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="other" if changed == "rack" else "target",
                rack_face="B" if changed == "face" else "A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.version += 1
        host.outbound.settings[1] = replace(
            host.outbound.settings[1], work_line_version=line.version
        )
        if changed == "scanner":
            line.device_bindings = {**line.device_bindings, "PLATFORM_SCANNER": "other-scanner"}
        elif changed == "rack_arm":
            arm = await session.scalar(
                select(Device).where(Device.device_code == line.device_bindings["RACK_ARM"])
            )
            arm.version += 1
        elif changed == "transfer_point":
            line.position_bindings = {**line.position_bindings, "TRANSFER_RACK": "Q"}
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert (plate.put_command_code is not None) == (changed == "scanner")
        if plate.put_command_code:
            job = await host.dispatch.progress(session, job_id=plate.target_rack_job_id)
            assert job.origin.resources["work_line_version"] == 1
            assert (
                plate.source_arrival_snapshot["put_command"]["device_code"]
                == line.device_bindings["RACK_ARM"]
            )


@pytest.mark.parametrize("invalid_next", [False, True])
async def test_target_rotation_requires_original_applied_action(decided_outbound, invalid_next):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_preparation="ROTATE",
            **({"next_source_action": {"cell_id": "broken"}} if invalid_next else {}),
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="B",
                slot_id="B1",
                rack_layer=2,
                rack_column=3,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code is None
        await racks.advance(session, task_id="t", now_ms=70)
        assert plate.target_preparation_action_id
        job = await host.dispatch.progress(session, job_id=plate.target_rack_job_id)
        action = next(a for a in job.actions if a.id == plate.target_preparation_action_id)
        assert action.kind == "ROTATE"
        assert not await host.outbound.target_clear(session, job_id=plate.target_rack_job_id)
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=80)
        assert plate.put_command_code is None
        await apply_outcome(
            session, action, "SUCCEEDED", point="P", rack_id="target", arrival_face="B"
        )
        await racks.advance(session, task_id="t", now_ms=90)
        assert not await host.outbound.target_clear(session, job_id=plate.target_rack_job_id)
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=100)
        assert plate.put_command_code


@pytest.mark.parametrize(
    ("placed", "disposition"), [(False, "CLOSE"), (True, "CONTINUE"), (True, "CLOSE")]
)
async def test_rejected_plate_uses_line_bin_arm_and_requires_original_ng_put(
    decided_outbound, placed, disposition
):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="REJECT",
            business_exception_code="MATERIAL_REJECTED",
            source_disposition=disposition,
            ng_locator=dict(type="NG_ZONE", zone_code="MATERIAL_NG_01"),
        ),
    )
    async with host.factory.begin() as session:
        for now in (60, 70):
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now
            )
        assert plate.final_decision_round_id
        assert plate.put_command_code
        code = plate.put_command_code
        line = await session.get(WorkLine, 1)
        assert (
            plate.source_arrival_snapshot["put_command"]["device_code"]
            == line.device_bindings["BIN_ARM"]
        )
        assert plate.source_arrival_snapshot["put_command"]["params"] == dict(
            source=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
            target=dict(location_id="STATION_NG", location_type="SCAN_NG"),
        )
        assert plate.report_message_id is None
        assert plate.pending
        assert not await host.outbound.closed(session, plate)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )

    if placed:
        assert (await host.arm_result(code)).status_code == 200
    else:
        assert (await host.arm_result(code, status="FAILED", empty="PUT_FAILED")).status_code == 200
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=80
        )
        assert plate.put_command_code == code
        if not placed:
            assert plate.pending and plate.report_message_id is None
            assert not await host.outbound.closed(session, plate)
            return
        assert plate.report_data["to_locator"] == dict(type="NG_ZONE", zone_code="MATERIAL_NG_01")
        assert plate.report_data["occurred_at"] == 99
        assert not await host.outbound.closed(session, plate)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        plate = await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=90)
        assert await host.outbound.closed(session, plate)
        assert plate.pending

        assert (plate.source_closed_basis is not None) is (disposition == "CLOSE")
    async with host.factory.begin() as session:
        for now in (100, 110):
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now
            )
        assert not plate.pending
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.trigger_plate_id == plate.id,
                )
            )
            == 1
        )
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=120
        )
        assert await host.outbound.bin_closed(session, work)


async def test_return_faces_reuse_original_job_only_after_terminal_face(outbound_host):
    from app.businesses.smt_auto.models import AutoOutboundSourceMember
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = outbound_host
    racks = OutboundRacks(host.outbound, station_id="station")
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        members = [
            AutoOutboundSourceMember(
                task_id="t",
                plan_input_id=plan.id,
                plan_revision=1,
                rack_id="return",
                rack_face=face,
                rack_array_index=0,
            )
            for face in ("B", "A")
        ]
        session.add_all(members)
        await session.flush()
        await racks.advance(session, task_id="t", now_ms=20)
        a, b = members[1], members[0]
        assert a.rack_job_id and b.rack_job_id is None
        ids = a.id, b.id
        job = await host.dispatch.progress(session, job_id=a.rack_job_id)
        await apply_outcome(
            session, job.actions[-1], "SUCCEEDED", point="Q", rack_id="return", arrival_face="A"
        )
        await racks.advance(session, task_id="t", now_ms=30)
    await host.settle("outbound.return_rack.work_plan@v1", dict(result="NO_WORK"))
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=40)
        await racks.advance(session, task_id="t", now_ms=50)
        a, b = [await session.get(AutoOutboundSourceMember, identity) for identity in ids]
        assert a.rack_job_id == b.rack_job_id
        assert b.preparation_action_id
        job = await host.dispatch.progress(session, job_id=b.rack_job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "ROTATE"]
        assert job.actions[-1].target_face == "B"


async def test_empty_wire_keeps_bin_unbound_and_rack_original_revision(outbound_host):
    from pydantic import ValidationError

    from app.businesses.smt_auto.request.outbound_source_empty_decide_v1 import EmptyDecide
    from app.integrations.model_payload import validated_model_payload

    bin_data = dict(
        task_id="t",
        source_locator=dict(type="BIN_CELL", bin_code="B1", cell_id="C1"),
        observed_at=99,
    )
    rack_data = dict(
        task_id="t",
        source_locator=dict(type="RACK_SLOT", rack_id="R1", rack_face="A", slot_id="S1"),
        observed_at=99,
        plan_revision=1,
    )
    for data in (bin_data, rack_data):
        assert validated_model_payload(EmptyDecide.model_validate(data)) == data
    for data in (
        {**bin_data, "plan_revision": 1},
        {**bin_data, "plan_revision": None},
        {k: v for k, v in rack_data.items() if k != "plan_revision"},
    ):
        with pytest.raises(ValidationError):
            EmptyDecide.model_validate(data)


async def test_target_replacement_waits_for_applied_movement_report(decided_outbound):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    next_source = dict(
        cell_id="C2", cell_index=2, bin_type=3, reel_layer=1, reel_thickness=20, reel_diameter=7
    )
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
            next_source_action=next_source,
        ),
    )
    async with host.factory.begin() as session:
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = first.put_command_code
        assert put_code
        second = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.grant_parent_plate_id == first.id)
        )
        second_id = second.id
        await host.outbound.advance(session, work_line_id=1, plate_id=second.id, now_ms=70)
        assert second.pick_command_code is None
        job_id = first.target_rack_job_id
        assert not any(
            action.kind == "DEPART"
            for action in (await host.dispatch.progress(session, job_id=job_id)).actions
        )
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        second = await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=75)
        pick_code = second.pick_command_code
        assert pick_code
    await host.arm_result(pick_code, scan=True)
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=80)
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_preparation="REPLACE",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="replacement",
                rack_face="A",
                slot_id="A2",
                rack_layer=2,
                rack_column=2,
            ),
        ),
    )
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=90)
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=110
        )
        assert first.report_message_id and first.report_consumed_at_ms is None
        demand, handoff = await racks.ports.facts(session, job_id=job_id)
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"
        final = await session.get(AutoOutboundDecisionRound, first.final_decision_round_id)
        final.unresolved = True
        await session.flush()
        assert (await racks.ports.facts(session, job_id=job_id))[0].state == "CONTINUE"
        final.unresolved = False
        await session.flush()
        await racks.advance(session, task_id="t", now_ms=120)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER"]
        second = await session.get(AutoOutboundPlate, second_id)
        assert second.target_rack_job_id is None
        assert second.put_command_code is None
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=130)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER"]
        await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=131)
        await racks.advance(session, task_id="t", now_ms=132)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "DEPARTURE_DECIDE"]
    await host.settle(
        "rack.departure_decide@v1",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "T_OUT"}},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=140)
        await racks.advance(session, task_id="t", now_ms=141)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "DEPARTURE_DECIDE", "DEPART"]


async def test_final_decision_and_put_intent_roll_back_together(decided_outbound):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    async with host.factory() as session:
        await session.begin()
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code
        await session.rollback()
    async with host.factory.begin() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        assert plate.put_command_code is None
        assert plate.final_decision_round_id is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 1
        )
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=70)
        assert plate.put_command_code
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )


async def test_transfer_demand_requires_complete_original_plan_coverage(decided_outbound):
    host = decided_outbound
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        assert (await ports.facts(session, job_id=plan.target_job_id))[0].state == "CONTINUE"
        plan.effective_request = {
            **plan.effective_request,
            "added_return_racks": [{"rack_id": "missing", "rack_faces": ["A"]}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plan.target_job_id))[0].state == "UNKNOWN"


async def test_replacement_with_next_does_not_wait_for_its_own_successor_decision(decided_outbound):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_preparation="REPLACE",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="replacement",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
            next_source_action=dict(
                cell_id="C2",
                cell_index=2,
                bin_type=3,
                reel_layer=1,
                reel_thickness=15.3,
                reel_diameter=7,
            ),
        ),
    )
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=60)
        successor = await session.scalar(
            select(AutoOutboundPlate).where(
                AutoOutboundPlate.grant_parent_plate_id == host.plate_id
            )
        )
        assert successor and successor.final_decision_round_id is None
        plan = await session.scalar(select(AutoTaskInput))
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plan.target_job_id)
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")


@pytest.mark.parametrize("outbound_host", [("ARM02", "ARM01", "NO_BIN")], indirect=True)
async def test_target_only_waits_for_production_sources(outbound_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = outbound_host
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        job_id = plan.target_job_id
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        assert (await ports.facts(session, job_id=job_id))[0].state == "CONTINUE"
        for now in (20, 30):
            assert await flow.advance(session, task_id="t", now_ms=now) == "WAITING"
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        assert not await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        assert (await ports.facts(session, job_id=job_id))[0].state == "CONTINUE"


@pytest.mark.parametrize("outbound_host", [("ARM02", "ARM01", "NO_BIN")], indirect=True)
async def test_unstarted_withdrawn_supply_can_confirm_without_production(outbound_host):
    from app.businesses._components.sorting_task.contracts import Cancel, PlanDelta
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    host = outbound_host
    service = AutoTaskService(roller=host.roller)
    delta = PlanDelta(
        task_id="t",
        plan_revision=2,
        added_bin_source_racks=[{"rack_id": "unused", "rack_face": ["A"]}],
    )
    cancel = Cancel(
        task_id="t",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"plan_revision": 2, "rack_id": "unused", "rack_face": ["A"]}],
    )
    async with host.factory.begin() as session:
        for name, payload, accept in (
            ("plan_delta", delta, service.accept_task_plan_delta),
            ("cancel", cancel, service.accept_task_cancel),
        ):
            await accept(
                CallbackContext(
                    session,
                    1,
                    f"supply-{name}",
                    50,
                    payload.model_dump(mode="json", exclude_none=True),
                    operation=f"sorting.task.{name}@v1",
                ),
                payload,
            )
        flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"


async def test_mixed_withdrawn_sources_release_target_before_report(decided_outbound):
    from app.businesses._components.sorting_task.contracts import Cancel, PlanDelta
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    host = decided_outbound
    service = AutoTaskService(roller=host.roller)
    async with host.factory.begin() as session:
        delta = PlanDelta(
            task_id="t",
            plan_revision=2,
            added_return_racks=[{"rack_id": "unused-return", "rack_faces": ["A"]}],
            added_bin_source_racks=[{"rack_id": "unused-supply", "rack_face": ["A"]}],
        )
        await service.accept_task_plan_delta(
            CallbackContext(
                session,
                1,
                "mixed-plan",
                51,
                delta.model_dump(mode="json", exclude_none=True),
                operation="sorting.task.plan_delta@v1",
            ),
            delta,
        )
        cancel = Cancel(
            task_id="t",
            cancel_scope="PLAN_MEMBERS",
            direct_pick_sources=[
                {"plan_revision": 2, "rack_id": "unused-return", "rack_face": "A"}
            ],
            bin_source_racks=[{"plan_revision": 2, "rack_id": "unused-supply", "rack_face": ["A"]}],
        )
        await service.accept_task_cancel(
            CallbackContext(
                session,
                1,
                "mixed-cancel",
                52,
                cancel.model_dump(mode="json", exclude_none=True),
                operation="sorting.task.cancel@v1",
            ),
            cancel,
        )
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        code = plate.put_command_code
        assert code
    await host.arm_result(code)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=70
        )
        assert plate.report_message_id and plate.report_consumed_at_ms is None
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plate.target_rack_job_id)
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")
        cancellation = await session.scalar(
            select(AutoTaskInput).where(AutoTaskInput.operation_id == "mixed-cancel")
        )
        original_cancellation = cancellation.effective_request
        cancellation.effective_request = {
            **cancellation.effective_request,
            "direct_pick_sources": [{"plan_revision": 2, "rack_id": "wrong", "rack_face": "A"}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plate.target_rack_job_id))[0].state == "CONTINUE"
        cancellation.effective_request = {
            **original_cancellation,
            "bin_source_racks": [{"plan_revision": 2, "rack_id": "wrong", "rack_face": ["A"]}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plate.target_rack_job_id))[0].state == "CONTINUE"


@pytest.mark.parametrize("scanned", [False, True])
async def test_original_pick_failed_wait_retry_and_source_done(outbound_host, scanned):
    from app.businesses.smt_auto.models import AutoOutboundDecisionRound

    host = outbound_host
    ready = dict(
        result="READY",
        cell_id="C1",
        cell_index=1,
        bin_type=3,
        reel_layer=2,
        reel_thickness=15.3,
        reel_diameter=7,
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=20)
    await host.settle("outbound.bin.work_plan@v1", ready)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=30)
        plate = await session.scalar(select(AutoOutboundPlate))
        first_id = plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=first_id, now_ms=40)
        first_code = plate.pick_command_code

    async def advance(plate_id, now_ms):
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=now_ms)

    await advance(first_id, 100)
    async with host.factory() as session:
        assert (
            await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE"
                )
            )
            is None
        )
    if scanned:
        assert (await host.platform_scan(first_code)).status_code == 200
    assert (
        await host.arm_result(first_code, status="FAILED", empty="PICK_FAILED")
    ).status_code == 200
    await advance(first_id, 110)
    if scanned:
        async with host.factory() as session:
            assert not await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose.in_(("EMPTY_DECIDE", "MATERIAL_DECIDE"))
                )
            )
        return
    await host.settle("outbound.source.empty_decide@v1", dict(result="WAIT", retry_after_ms=20))
    await advance(first_id, 120)
    await advance(first_id, 139)
    async with host.factory() as session:
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE",
                )
            )
        )
        assert len(rounds) == 1
    await advance(first_id, 140)
    await host.settle("outbound.source.empty_decide@v1", dict(result="RETRY"))
    await advance(first_id, 150)
    await advance(first_id, 151)
    async with host.factory.begin() as session:
        first = await session.get(AutoOutboundPlate, first_id)
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE",
                )
                .order_by(AutoOutboundDecisionRound.round_no)
            )
        )
        assert [r.request_data for r in rounds] == [
            dict(task_id=first.task_id, source_locator=first.source_locator, observed_at=99)
        ] * 2
        assert rounds[0].operation_id != rounds[1].operation_id
        assert rounds[1].previous_round_id == rounds[0].id
        retry = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.grant_round_id == rounds[1].id)
        )
        assert retry.source_locator == first.source_locator
        assert retry.source_params == first.source_params
        retry_id = retry.id
        await host.outbound.advance(session, work_line_id=1, plate_id=retry_id, now_ms=160)
        assert retry.pick_command_code and retry.pick_command_code != first_code
        retry_code = retry.pick_command_code
    assert (
        await host.arm_result(retry_code, status="FAILED", empty="PICK_FAILED")
    ).status_code == 200
    await advance(retry_id, 170)
    await host.settle("outbound.source.empty_decide@v1", dict(result="SOURCE_DONE"))
    await advance(retry_id, 180)
    await advance(retry_id, 181)
    async with host.factory() as session:
        work = await session.get(AutoBinWork, host.work_id)
        assert not await host.outbound.bin_closed(session, work)
    await host.settle("outbound.bin.work_plan@v1", dict(result="WAIT", retry_after_ms=1))
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=190
        )
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=191)
    await host.settle("outbound.bin.work_plan@v1", dict(result="NO_WORK"))
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=200
        )
        work = await session.get(AutoBinWork, host.work_id)
        assert await host.outbound.bin_closed(session, work)
        plates = list(await session.scalars(select(AutoOutboundPlate)))
        assert len(plates) == 2
        assert all(not p.pending and p.report_message_id is None for p in plates)


@pytest.mark.parametrize(
    "stage,next_slot,conflict",
    [
        ("PUT_FAILED", None, False),
        ("PICK_FAILED", "S1", False),
        ("PICK_FAILED", "S2", False),
        ("PICK_FAILED", "S2", True),
    ],
)
async def test_return_slot_empty_only_for_original_pick_failure(
    outbound_host, stage, next_slot, conflict
):
    from app.businesses.smt_auto.models import AutoOutboundDecisionRound, AutoOutboundSourceMember
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = outbound_host
    racks = OutboundRacks(host.outbound, station_id="station")
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        member = AutoOutboundSourceMember(
            task_id="t",
            plan_input_id=plan.id,
            plan_revision=1,
            rack_id="return",
            rack_face="A",
            rack_array_index=0,
        )
        session.add(member)
        await session.flush()
        member_id = member.id
        await racks.advance(session, task_id="t", now_ms=20)
        progress = await host.dispatch.progress(session, job_id=member.rack_job_id)
        await apply_outcome(
            session,
            progress.actions[-1],
            "SUCCEEDED",
            point="Q",
            rack_id="return",
            arrival_face="A",
        )
        await racks.advance(session, task_id="t", now_ms=30)
    ready = dict(
        result="READY",
        rack_id="return",
        rack_face="A",
        slot_id="S1",
        rack_layer=2,
        rack_column=3,
        reel_thickness=15.3,
        reel_diameter=7,
    )
    await host.settle("outbound.return_rack.work_plan@v1", ready)
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=40)
        plate = await session.scalar(select(AutoOutboundPlate))
        plate_id = plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        code = plate.pick_command_code
    result = await host.arm_result(code, status="FAILED", empty=stage)
    assert result.status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound).where(
                AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE"
            )
        )
        if stage != "PICK_FAILED":
            assert round_ is None
            return
        assert round_.request_data == dict(
            task_id="t",
            source_locator=dict(type="RACK_SLOT", rack_id="return", rack_face="A", slot_id="S1"),
            plan_revision=1,
            observed_at=99,
        )
    await host.settle("outbound.source.empty_decide@v1", dict(result="SOURCE_DONE"))
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert not await host.outbound.face_source_terminal(session, member)
    await host.settle("outbound.return_rack.work_plan@v1", dict(result="WAIT", retry_after_ms=1))
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=80)
        await racks.advance(session, task_id="t", now_ms=81)
    if conflict:
        changed = await host.arm_result(code, status="FAILED", empty="PUT_FAILED")
        assert changed.status_code == 409
    await host.settle(
        "outbound.return_rack.work_plan@v1",
        {**ready, "slot_id": next_slot, "rack_column": 4 if next_slot == "S2" else 3},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=90)
        plates = list(
            await session.scalars(select(AutoOutboundPlate).order_by(AutoOutboundPlate.id))
        )
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound)
            .where(
                AutoOutboundDecisionRound.operation == "outbound.return_rack.work_plan@v1",
            )
            .order_by(AutoOutboundDecisionRound.round_no.desc())
        )
        assert round_.request_data == dict(
            task_id="t",
            rack_id="return",
            rack_face="A",
            arrived_at=30,
        )
        if next_slot == "S1":
            assert [p.id for p in plates] == [plate_id]
            assert round_.unresolved
        else:
            original, successor = plates
            assert original.id == plate_id
            assert successor.source_locator["slot_id"] == next_slot
            assert successor.source_params["rack_column"] == 4
            await host.outbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=91)
            if conflict:
                assert successor.pick_command_code is None
            else:
                assert successor.pick_command_code and successor.pick_command_code != code
