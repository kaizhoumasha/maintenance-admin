"""Outbound consumes original ECS results and WMS outbox responses."""

# This module intentionally re-exports test symbols to keep split files small.
# ruff: noqa: F401

import asyncio
import importlib
import json
import logging
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from time import time_ns
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import business_wakeups
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
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL, EcsCommandAdapter
from app.integrations.ecs_command.result import receive_ecs_result
from app.integrations.ecs_command.service import CommandOwner
from app.integrations.wms_request import ADAPTERS, FACTS_PROTOCOL
from app.models import (
    Device,
    DeviceRoute,
    IntegrationExchange,
    IntegrationRecord,
    ReliableMessage,
    WorkLine,
    WorkLinePosition,
)
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.services.device_bindings import resolve_binding
from tests.businesses.rack_dispatch import apply_outcome, origin, respond
from tests.helpers import read_history_body


class _TrackingFactory:
    def __init__(self, factory):
        self.factory = factory
        self.active = 0

    def __call__(self, *args, **kwargs):
        context = self.factory(*args, **kwargs)
        tracker = self

        class Context:
            async def __aenter__(self):
                session = await context.__aenter__()
                tracker.active += 1
                return session

            async def __aexit__(self, *exc_info):
                try:
                    return await context.__aexit__(*exc_info)
                finally:
                    tracker.active -= 1

        return Context()


@pytest.fixture(params=[("ARM02", "ARM01"), ("ARM04", "ARM03")])
async def outbound_host(s5_roller, request, monkeypatch):
    host = s5_roller
    rack_arm, bin_arm = request.param[:2]
    target_rack, target_point, target_face = (
        request.param[3] if len(request.param) > 3 else ("target", "P", "A")
    )
    no_target = len(request.param) > 2 and request.param[2] == "NO_TARGET"
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
        line.position_bindings = {"TRANSFER_RACK": target_point, "RETURN_RACK": "Q"}
        session.add(WorkLinePosition(work_line_id=1, position_code=target_point, capacity=None))
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
            effective_request=(
                {}
                if no_target
                else {"target_rack": {"rack_id": target_rack, "rack_face": target_face}}
            ),
        )
        session.add(plan)
        await session.flush()
        if no_target:
            binding = None
        else:
            binding = await resolve_binding(session, rack_arm)
        if no_target:
            pass
        else:
            auth = RackAuthorization("FIRST_TARGET", plan.id)
            job_origin = replace(
            origin("AUTO_OUTBOUND_TARGET"),
            work_line_id=1,
            task_id="t",
            source_ref=auth.source_ref,
            initial_face=target_face,
            rack_id=target_rack,
            point=target_point,
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
            face=target_face,
            original_binding=binding,
        )
            progress = await host.dispatch.progress(session, job_id=job)
            await apply_outcome(
            session,
            progress.actions[-1],
            "SUCCEEDED",
            point=target_point,
            rack_id=target_rack,
            arrival_face=target_face,
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
        settings=PhysicalSettings(
            1, 1, 30001, 30002, ((target_point, "STATION_A"), ("Q", "STATION_B"))
        ),
    )
    host.ecs = ecs
    host.work_plans.outbound = host.outbound
    host.work_plans.roller = host.roller

    pending_claims = []

    async def settle(operation, data, code="DECIDED", *, on_delivered=None, operation_id=None):
        store = MessageStore(
            host.factory,
            records=history_records,
            on_delivered=on_delivered,
            on_result=business_wakeups.wms_result_committed if on_delivered else None,
        )
        pending_claims.extend(await store.claim(100, host.requests.outbox.targets))
        claims = pending_claims
        matching = [
            c
            for c in claims
            if c.message.operation == operation
            and (operation_id is None or c.message.operation_id == operation_id)
        ]
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

    async def result(
        code, *, scan=False, status="SUCCESS", empty=False, legacy_scan=False, **extra
    ):
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
                data=data | extra,
            )
            if status == "FAILED":
                body["error_detail"] = dict(code="STOPPED", msg="stopped")
            response = await receive_ecs_result(session, json.dumps(body).encode(), ecs)
        if separate_scan:
            assert (await platform_scan(code)).status_code == 200
        return response

    async def ack(code, *, on_ack=None):
        store = MessageStore(
            host.factory,
            lifecycles={PROTOCOL: EcsLifecycle(ecs, on_ack=on_ack)},
            records=history_records,
        )
        (claim,) = await store.claim(1, ecs.outbox.targets)
        assert claim.message.operation_id == code
        assert await store.before_send(claim, ecs.outbox.targets["ecs"])
        response = HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            200,
            (("content-type", "application/json"),),
            b'{"code":200,"message":"ACK"}',
        )
        verdict = EcsCommandAdapter().classify_response(claim.message, response)
        assert await store.finish(
            claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
        )

    async def platform_scan(
        code, *, barcode="原始料盘," * 60, timestamp=88, device="SCANNER_001", scan_revision=None
    ):
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
        if scan_revision is not None:
            body["data"]["scan_revision"] = scan_revision
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
    host.arm_ack = ack
    host.settle, host.arm_result = settle, result
    yield host
    for name, namespace in original_modules.items():
        if namespace is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name].__dict__.clear()
            sys.modules[name].__dict__.update(namespace)


async def return_face(host, *, ready=True, consume=True):
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
    if ready is not None:
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
                size_inch=13,
                reel_diameter=330.2,
            )
            if ready is True
            else ready
            if ready
            else {"result": "WAIT", "retry_after_ms": 100},
        )
    async with host.factory.begin() as session:
        if consume:
            await racks.advance(session, task_id="t", now_ms=40)
        plate = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.source_member_id == member_id)
        )
        return racks, member_id, plate.id if plate else None


@pytest.fixture
async def decided_outbound(outbound_host, request):
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
            size_inch=7,
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
    original_barcode = getattr(request, "param", None)
    if original_barcode is None:
        await host.arm_result(code, scan=True)
    else:
        await host.arm_result(code)
        assert (await host.platform_scan(code, barcode=original_barcode)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    return host


async def _empty_pending_page():
    return ()


def accepted_target(*, rack_id="target", rack_face="A", preparation=None):
    decision = dict(
        result="ACCEPT",
        target_locator=dict(
            type="RACK_SLOT",
            rack_id=rack_id,
            rack_face=rack_face,
            slot_id="A1",
            rack_layer=1,
            rack_column=1,
        ),
    )
    if preparation is not None:
        decision["target_preparation"] = preparation
    return decision


def ready_bin(cell="C2", index=2):
    return dict(
        result="READY",
        cell_id=cell,
        cell_index=index,
        bin_type=3,
        reel_layer=1,
        reel_thickness=15.3,
        size_inch=7,
        reel_diameter=7,
    )


async def consume_bin_plan(host, session, *, now_ms, work_id=None):
    work = await session.get(AutoBinWork, work_id or host.work_id)
    return await host.work_plans.consume_bin(
        session, work_line_id=1, arrival_id=work.arrival_id, now_ms=now_ms
    )
