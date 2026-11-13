"""Normal inbound consumes actual ECS results and actual WMS outbox responses."""

import importlib
import json
import sys
from dataclasses import asdict, replace

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import (
    AutoBinWork,
    AutoInboundDecisionRound,
    AutoInboundPlate,
    AutoInboundSourceMember,
    AutoTask,
    AutoTaskInput,
)
from app.businesses.smt_auto.rack_ports import AutoRackPorts, RackAuthorization
from app.core.config import AutoWorklineSettings
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.ecs_command import compile_commands
from app.integrations.ecs_command.result import receive_ecs_result
from app.integrations.ecs_command.service import CommandOwner
from app.integrations.wms_request import ADAPTERS, FACTS_PROTOCOL
from app.models import (
    Device,
    DeviceRoute,
    IntegrationExchange,
    ReliableMessage,
    WorkLine,
    WorkLinePosition,
)
from app.reliable.config import TargetConfig
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.services.device_bindings import resolve_binding
from tests.businesses.rack_dispatch import apply_outcome, origin
from tests.helpers import read_history_body


@pytest.fixture
def arm_timeouts():
    return {"rack_arm_timeout_ms": 30001, "bin_arm_timeout_ms": 30002}


@pytest.fixture(params=[("ARM02", "ARM01"), ("ARM04", "ARM03")])
async def inbound_host(s5_roller, request, monkeypatch, arm_timeouts):
    host = s5_roller
    rack_arm, bin_arm = request.param[:2]
    for name in (
        "putaway_material_decide_v1",
        "putaway_material_movement_report_v1",
        "putaway_source_empty_decide_v1",
    ):
        module = f"app.businesses.smt_auto.request.{name}"
        if module in sys.modules:
            importlib.reload(sys.modules[module])
        else:
            importlib.import_module(module)
    from app.businesses.smt_auto.ecs_command import PickAndPut

    if "app.businesses.smt_auto.inbound" in sys.modules:
        importlib.reload(sys.modules["app.businesses.smt_auto.inbound"])
    from app.businesses.smt_auto.inbound import Inbound, PhysicalSettings

    ecs = host.roller.capabilities.ecs
    from app.businesses import catalog
    from app.businesses.smt_auto.definition import DEFINITION
    from app.integrations.wms_callback import registry as callbacks

    monkeypatch.setattr(callbacks, "_registry", {})
    from app.businesses._components.rack_dispatch.composition import COMPONENT as RACK_COMPONENT
    from app.businesses._components.sorting_task.composition import COMPONENT

    host.entry.COMPONENTS = (*host.entry.COMPONENTS, RACK_COMPONENT)
    original_rack_modules = {
        name: dict(sys.modules[name].__dict__) if name in sys.modules else None
        for name in RACK_COMPONENT.entry_modules
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

    host.entry.DEFINITION = replace(
        DEFINITION,
        initial_entries=tuple(e for e in DEFINITION.initial_entries if e.kind == "wms_callback"),
    )
    host.entry.COMMANDS = (PickAndPut,)
    host.entry.PROGRESSION = "scan_pending"
    name = "app.businesses.smt_auto.callback.putaway_source_rack_exhausted_v1"
    host.entry.ENTRY_MODULES = (*host.entry.ENTRY_MODULES, name)
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
        task.business_type, task.plan_revision = "inbound", 1
        line = await session.get(WorkLine, 1)
        line.version = 1
        line.device_bindings = {"RACK_ARM": rack_arm, "BIN_ARM": bin_arm, "SCAN2": "SCAN2"}
        line.position_bindings = {"INBOUND_LEFT": "P", "INBOUND_RIGHT": "Q"}
        session.add(WorkLinePosition(work_line_id=1, position_code="P", capacity=None))
        session.add(WorkLinePosition(work_line_id=1, position_code="Q", capacity=None))
        for index, code in enumerate((rack_arm, bin_arm, "SCAN2"), start=1):
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
            effective_request={"added_source_racks": [{"rack_id": "rack"}, {"rack_id": "other"}]},
        )
        session.add(plan)
        await session.flush()
        for index, (rack_id, point) in enumerate((("rack", "P"), ("other", "Q"))):
            member = AutoInboundSourceMember(
                task_id="t",
                plan_input_id=plan.id,
                plan_revision=1,
                rack_id=rack_id,
                rack_array_index=index,
            )
            session.add(member)
            await session.flush()
            if len(request.param) > 2:
                continue
            binding = await resolve_binding(session, rack_arm)
            auth = RackAuthorization("INBOUND_MEMBER", member.id)
            job_origin = replace(
                origin("AUTO_INBOUND_SOURCE"),
                work_line_id=1,
                task_id="t",
                source_ref=auth.source_ref,
                initial_face=None,
                rack_id=rack_id,
                point=point,
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
                face=None,
                original_binding=binding,
            )
            progress = await host.dispatch.progress(session, job_id=job)
            await apply_outcome(
                session, progress.actions[-1], "SUCCEEDED", point=point, rack_id=rack_id
            )
            await ports.advance(session, job_id=job, now_ms=10)
            assert action
    work = await host.scan()
    host.work_id = work.id
    line_settings = AutoWorklineSettings(
        work_line_id=1,
        work_line_version=1,
        source_locations=(("P", "STATION_A"), ("Q", "STATION_B")),
        **arm_timeouts,
    )
    host.inbound = Inbound(
        host.requests,
        ecs,
        host.dispatch,
        host.roller,
        settings=PhysicalSettings(
            line_settings.work_line_id,
            line_settings.work_line_version,
            line_settings.rack_arm_timeout_ms,
            line_settings.bin_arm_timeout_ms,
            line_settings.source_locations,
        ),
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

    async def result(code, *, scan=False, status="SUCCESS", empty=False, **extra):
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
            if scan:
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
            return await receive_ecs_result(session, json.dumps(body).encode(), ecs)

    host.settle, host.arm_result = settle, result
    yield host
    for name, namespace in original_rack_modules.items():
        if namespace is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name].__dict__.clear()
            sys.modules[name].__dict__.update(namespace)


@pytest.mark.parametrize(
    "next_invalid,put_failed,arm_timeouts",
    [
        (False, False, {}),
        (True, False, {"rack_arm_timeout_ms": 30001}),
        ("VALID", False, {"bin_arm_timeout_ms": 30002}),
        (False, True, {"rack_arm_timeout_ms": 30001, "bin_arm_timeout_ms": 30002}),
    ],
)
async def test_one_normal_plate_original_public_physical_report_chain(
    inbound_host, next_invalid, put_failed, arm_timeouts
):
    host = inbound_host
    source = dict(
        rack_id="rack",
        location_code="P",
        bin_code="SOURCE",
        bin_location="B",
        cell_id="CELL",
        bin_type="TYPE-A",
        cell_index=1,
        reel_layer=1,
        reel_thickness=20,
        reel_diameter=381,
        reel_totalthickness=20,
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20
        )
    await host.settle("putaway.work_plan@v1", dict(result="READY", source=source))
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoInboundPlate))
        assert plate
        plate_id = plate.id
        await host.inbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=40)
        assert plate.pick_command_code
        pick_code = plate.pick_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == pick_code,
            )
        )
        raw = json.loads(await read_history_body(session, command.request_record_id))
        assert raw["params"]["source"]["reel_diameter"] == 15
        assert raw["params"]["source"]["bin_type"] == "TYPE-A"
        assert plate.source_params["reel_diameter"] == 381
        assert json.loads(await read_history_body(session, (command).request_record_id))[
            "timeout"
        ] == arm_timeouts.get("rack_arm_timeout_ms", 0)
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        assert plate.scanned_at_ms == 88
        assert plate.barcode == "原始料盘," * 60
        assert not await host.inbound.closed(session, plate)
    target = dict(
        bin_code="A000000001",
        cell_id="DEST",
        bin_type=9,
        cell_index=2,
        reel_layer=0,
        reel_thickness=20,
        reel_diameter=330.2,
        reel_totalthickness=0,
    )
    decision = dict(result="ACCEPT", target=target)
    if next_invalid:
        decision["next_source_action"] = (
            source if next_invalid == "VALID" else {"rack_id": "unconfirmed"}
        )
    await host.settle("putaway.material.decide@v1", decision)
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
        assert plate.put_command_code
        put_code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == put_code,
            )
        )
        raw = json.loads(await read_history_body(session, (command).request_record_id))
        assert raw["timeout"] == arm_timeouts.get("bin_arm_timeout_ms", 0)
        assert raw["params"]["target"]["reel_layer"] == 0
        assert raw["params"]["target"]["reel_totalthickness"] == 0
        assert raw["params"]["target"]["bin_type"] == "9"
        assert raw["params"]["target"]["reel_diameter"] == 13
        assert plate.target_snapshot["target"]["reel_diameter"] == 330.2
    assert (
        await host.arm_result(put_code, status="FAILED" if put_failed else "SUCCESS")
    ).status_code == 200
    if put_failed:
        async with host.factory.begin() as session:
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=70
            )
            assert plate.report_message_id is None
            assert plate.pending
            assert not await host.inbound.closed(session, plate)
        return
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        assert plate.report_data["occurred_at"] == 99
        assert not await host.inbound.closed(session, plate)
    await host.settle("putaway.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        plate = await host.inbound.advance_report(session, plate_id=plate_id, now_ms=80)
        assert await host.inbound.closed(session, plate)
        work = await session.get(AutoBinWork, host.work_id)
        assert await host.inbound.bin_closed(session, work) is (not next_invalid)
        if next_invalid:
            assert await session.scalar(select(func.count()).select_from(AutoInboundPlate)) == (
                2 if next_invalid == "VALID" else 1
            )
            return
        for now in (81, 90):
            await host.inbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=now)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 7
        from app.businesses.smt_auto.task_flow import TaskFlow

        flow = TaskFlow(
            host.requests, roller=host.roller, dispatch=host.dispatch, inbound=host.inbound
        )
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=100)
            == "RELEASING"
        )
        release_code = work.release_command_code
    assert (await host.result(release_code)).status_code == 200
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=110)
            == "RELEASED"
        )
        assert not await flow.original_inprocess(session, work_line_id=1, bin_code=work.bin_code)
        # Active rack membership remains an independent task obligation.
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        assert await flow.advance(session, task_id="t", now_ms=111) == "WAITING"
        from app.businesses.smt_auto.inbound_contracts import RackExhausted
        from app.integrations.wms_callback import CallbackContext

        for member in await session.scalars(select(AutoInboundSourceMember)):
            progress = await host.dispatch.progress(session, job_id=member.rack_job_id)
            action = next(a for a in progress.actions if a.id == member.preparation_action_id)
            ctx = CallbackContext(
                session,
                1,
                f"exhaust-{member.id}",
                112,
                {},
                operation="putaway.source.rack_exhausted@v1",
            )
            await host.inbound.accept_source_rack_exhausted(
                ctx,
                RackExhausted(
                    task_id="t", rack_id=member.rack_id, transport_task_id=action.transport_task_id
                ),
            )
        assert await flow.advance(session, task_id="t", now_ms=113) == "CONFIRMING"
    await host.settle("sorting.task.completion_confirm@v1", {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=114) == "COMPLETED"
        task = await session.get(AutoTask, "t")
        assert await flow.retention_basis(session, task, now_ms=115) == task.confirmation_basis
        from app.businesses.smt_auto.archive import RETENTION_MS, Archive
        from tests.businesses.rack_dispatch import respond

        ports = AutoRackPorts(host.dispatch, inbound=host.inbound)
        archive = Archive(flow, ports)
        member = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "rack")
        )
        job_id = member.rack_job_id
        progress = await ports.advance(session, job_id=job_id, now_ms=116)
        original = progress.actions[-1]
        assert original.kind == "DEPARTURE_DECIDE"
        assert await archive.clean(session, task_id=task.task_id, now_ms=114 + RETENTION_MS)
        assert task.details_cleaned
        assert await session.scalar(select(func.count()).select_from(AutoInboundPlate)) == 0
        assert (await ports.facts(session, job_id=job_id))[0].state == "NONE"
        await respond(
            session,
            original.decision_operation_id,
            200,
            "DECIDED",
            dict(result="READY", rack_destination=dict(type="ZONE", location_code="Z")),
        )
        after_archive = 115 + RETENTION_MS
        progress = await ports.advance(session, job_id=job_id, now_ms=after_archive)
        departure = progress.actions[-1]
        assert departure.kind == "DEPART"
        await respond(
            session,
            departure.submit_operation_id,
            202,
            "RECEIVED",
            dict(transport_task_id=departure.transport_task_id),
        )
        assert (
            await ports.advance(session, job_id=job_id, now_ms=after_archive)
        ).state == "COMPLETED"


@pytest.fixture
async def ready_inbound(inbound_host):
    host = inbound_host
    source = dict(
        rack_id="rack",
        location_code="P",
        bin_code="SOURCE",
        bin_location="B",
        cell_id="CELL",
        bin_type=3,
        cell_index=1,
        reel_layer=1,
        reel_thickness=20,
        reel_diameter=15,
        reel_totalthickness=20,
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20
        )
    await host.settle("putaway.work_plan@v1", dict(result="READY", source=source))
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoInboundPlate))
        host.plate_id = plate.id
    return host


@pytest.mark.parametrize("invalid", ["binding", "withdrawn", "original_job", "arrival"])
async def test_pick_requires_original_source_binding_and_arrival(ready_inbound, invalid):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await session.get(AutoInboundPlate, host.plate_id)
        if invalid == "binding":
            line = await session.get(WorkLine, 1)
            line.version += 1
        elif invalid == "withdrawn":
            member = await session.scalar(select(AutoInboundSourceMember))
            member.withdrawn_at_ms = 31
        elif invalid == "original_job":
            plate.source_rack_job_id = "different-original"
        else:
            passage = host.roller.tables["bin_passage"]
            await session.execute(
                passage.update()
                .where(passage.c.id == (await session.get(AutoBinWork, host.work_id)).arrival_id)
                .values(release_command_code="original-release-already-started")
            )
        await host.inbound.advance(session, work_line_id=1, plate_id=plate.id, now_ms=40)
        assert plate.pick_command_code is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 0
        )


async def test_rolled_back_pick_leaves_no_external_intent(ready_inbound):
    host = ready_inbound
    async with host.factory() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        assert plate.pick_command_code
        await session.rollback()
    async with host.factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 0
        )
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=50
        )
        assert plate.pick_command_code
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 1
        )


async def decide_exhaustion(host, *, consume=True, next_source=None):
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        pick_code = plate.pick_command_code
    await host.arm_result(pick_code, scan=True)
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    target = dict(
        bin_code="A000000001",
        cell_id="DEST",
        bin_type=3,
        cell_index=1,
        reel_layer=0,
        reel_thickness=20,
        reel_diameter=15,
        reel_totalthickness=0,
    )
    decision = dict(result="ACCEPT", target=target, source_rack_exhausted=True)
    if next_source is not None:
        decision["next_source_action"] = next_source
    await host.settle("putaway.material.decide@v1", decision)
    if consume:
        async with host.factory.begin() as session:
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=60
            )
            assert plate.put_command_code


async def add_bin_work(host, session, arrival):
    session.add(
        AutoBinWork(
            work_line_id=1,
            arrival_id=arrival,
            device_code="SCAN2",
            raw_bin_code=f"{arrival}-C",
            bin_code=arrival,
            scanned_at=61,
            task_id="t",
            business_type="inbound",
            association="TASK",
        )
    )
    await session.flush()
    return await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival, now_ms=70)


async def test_exhausted_original_source_is_removed_from_later_work_plan(ready_inbound):
    host = ready_inbound
    await decide_exhaustion(host)
    async with host.factory.begin() as session:
        later = await add_bin_work(host, session, "later")
        assert later.request_data["source_racks"] == [{"rack_id": "other", "location_code": "Q"}]
        plate = await session.get(AutoInboundPlate, host.plate_id)
        demand, handoff = await AutoRackPorts(host.dispatch, inbound=host.inbound).facts(
            session, job_id=plate.source_rack_job_id
        )
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"
        assert not await host.inbound.closed(session, plate)


async def test_late_ready_for_exhausted_original_stays_unconsumed_after_restart(ready_inbound):
    host = ready_inbound
    async with host.factory.begin() as session:
        later = await add_bin_work(host, session, "late")
        assert {s["rack_id"] for s in later.request_data["source_racks"]} == {"rack", "other"}
    await decide_exhaustion(host)
    source = dict(
        rack_id="rack",
        location_code="P",
        bin_code="SOURCE",
        bin_location="B",
        cell_id="CELL",
        bin_type=3,
        cell_index=1,
        reel_layer=1,
        reel_thickness=20,
        reel_diameter=15,
        reel_totalthickness=20,
    )
    await host.settle("putaway.work_plan@v1", dict(result="READY", source=source))
    from app.businesses.smt_auto.work_plans import WorkPlans

    restarted = WorkPlans(host.requests, host.dispatch)
    async with host.factory.begin() as session:
        for now in (80, 90):
            late = await restarted.consume_bin(
                session, work_line_id=1, arrival_id="late", now_ms=now
            )
            assert late.unresolved and late.consumed_at_ms is None
        assert await session.scalar(select(func.count()).select_from(AutoInboundPlate)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )


async def test_exhaustion_rollback_and_original_admission_identity(ready_inbound):
    host = ready_inbound
    await decide_exhaustion(host, consume=False)
    async with host.factory() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=plate.source_rack_job_id,
            preparation_action_id=plate.source_preparation_action_id,
        )
        await session.rollback()
    async with host.factory.begin() as session:
        plate = await session.get(AutoInboundPlate, host.plate_id)
        assert not await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=plate.source_rack_job_id,
            preparation_action_id=plate.source_preparation_action_id,
        )
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=70)
        assert await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=plate.source_rack_job_id,
            preparation_action_id=plate.source_preparation_action_id,
        )
        from app.core.identifiers import uuid7

        original = await host.dispatch.progress(session, job_id=plate.source_rack_job_id)
        new_job_id = await host.dispatch.register(
            session,
            replace(
                original.origin, cycle_id=str(uuid7()), source_ref="INBOUND_MEMBER:later-admission"
            ),
        )
        new_job = await host.dispatch.progress(session, job_id=new_job_id)
        assert new_job.id != original.id
        assert new_job.origin.rack_id == original.origin.rack_id
        assert not await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=new_job.id,
            preparation_action_id=plate.source_preparation_action_id,
        )
        assert not await host.work_plans.inbound_source_exhausted(
            session,
            task_id="other-task",
            job_id=plate.source_rack_job_id,
            preparation_action_id=plate.source_preparation_action_id,
        )


async def test_other_rack_material_decision_uses_latest_nonexhausted_scope(ready_inbound):
    host = ready_inbound
    next_source = dict(
        rack_id="other",
        location_code="Q",
        bin_code="OTHER",
        bin_location="B",
        cell_id="OTHER-CELL",
        bin_type=3,
        cell_index=1,
        reel_layer=1,
        reel_thickness=20,
        reel_diameter=15,
        reel_totalthickness=20,
    )
    await decide_exhaustion(host, next_source=next_source)
    async with host.factory.begin() as session:
        successor = await session.scalar(
            select(AutoInboundPlate).where(AutoInboundPlate.grant_parent_plate_id == host.plate_id)
        )
        assert successor
        await host.inbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=70)
        assert successor.pick_command_code
        successor_id, pick_code = successor.id, successor.pick_command_code
    await host.arm_result(pick_code, scan=True)
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=successor_id, now_ms=80)
        round_ = await session.scalar(
            select(AutoInboundDecisionRound).where(
                AutoInboundDecisionRound.plate_id == successor_id
            )
        )
        assert round_.request_data["source"] == dict(
            rack_id="other", bin_code="OTHER", cell_id="OTHER-CELL"
        )
        assert round_.request_data["source_racks"] == [{"rack_id": "other", "location_code": "Q"}]


async def test_material_wait_retries_only_at_original_due_and_preserves_pick(ready_inbound):
    host = ready_inbound
    operation = "putaway.material.decide@v1"

    async def advance(session, now_ms):
        return await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=now_ms
        )

    async def rounds(session):
        return (
            await session.scalars(
                select(AutoInboundDecisionRound)
                .where(AutoInboundDecisionRound.plate_id == host.plate_id)
                .order_by(AutoInboundDecisionRound.round_no)
            )
        ).all()

    async with host.factory.begin() as session:
        pick_code = (await advance(session, 40)).pick_command_code
    assert (await host.arm_result(pick_code, scan=True, location=None)).status_code == 200
    async with host.factory.begin() as session:
        await advance(session, 50)
        (first,) = await rounds(session)
        original = first.request_data
        original_operation_id = first.operation_id
        assert [item["rack_id"] for item in original["source_racks"]] == ["rack", "other"]
        # An unknown original outcome is not a WAIT, even long after its send.
        await advance(session, 59)
        assert [r.operation_id for r in await rounds(session)] == [original_operation_id]

    await host.settle(operation, {"result": "WAIT", "retry_after_ms": 100})
    async with host.factory.begin() as session:
        assert (await advance(session, 60)).put_command_code is None
        (first,) = await rounds(session)
        assert (first.consumed_at_ms, first.wait_recorded_at_ms, first.next_eligible_at_ms) == (
            60,
            60,
            160,
        )
        member = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "other")
        )
        member.withdrawn_at_ms = 70

    async with host.factory.begin() as session:
        assert (await advance(session, 159)).put_command_code is None
        (first,) = await rounds(session)
        assert first.request_data == original
        assert first.next_eligible_at_ms == 160
        outbox_count = await session.scalar(select(func.count()).select_from(ReliableMessage))

    async with host.factory() as session:
        await advance(session, 160)
        assert len(await rounds(session)) == 2
        await session.rollback()

    async with host.factory.begin() as session:
        assert len(await rounds(session)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ReliableMessage)) == outbox_count
        )
        await advance(session, 160)
        first, second = await rounds(session)
        assert second.previous_round_id == first.id
        assert second.operation_id != original_operation_id
        assert second.request_data == {**original, "source_racks": original["source_racks"][:1]}
        assert set(second.source_rack_bindings) == {"rack"}
        second_operation_id = second.operation_id

    await host.settle(operation, {"result": "WAIT", "retry_after_ms": 20})
    async with host.factory.begin() as session:
        assert (await advance(session, 170)).put_command_code is None
        await advance(session, 189)
        first, second = await rounds(session)
        assert first.next_eligible_at_ms == 160
        assert second.next_eligible_at_ms == 190
        assert second.operation_id == second_operation_id
        await advance(session, 190)
        await advance(session, 999)
        first, second, third = await rounds(session)
        assert third.previous_round_id == second.id
        assert third.request_data == second.request_data
        assert third.unresolved

    await host.settle(
        operation,
        {
            "result": "ACCEPT",
            "target": dict(
                bin_code="A000000001",
                cell_id="DEST",
                bin_type=3,
                cell_index=1,
                reel_layer=0,
                reel_thickness=20,
                reel_diameter=15,
                reel_totalthickness=0,
            ),
        },
    )
    async with host.factory.begin() as session:
        plate = await advance(session, 1000)
        assert plate.pick_command_code == pick_code
        assert plate.put_command_code
        first, second, third = await rounds(session)
        assert plate.final_decision_round_id == third.id
        assert [r.next_eligible_at_ms for r in (first, second, third)] == [160, 190, None]
        assert (await advance(session, 2000)).put_command_code == plate.put_command_code
        assert len(await rounds(session)) == 3
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )
        put_code, final_round_id = plate.put_command_code, third.id

    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        await advance(session, 2010)
    await host.settle("putaway.material.movement_report@v1", {}, "RECORDED")
    from app.businesses.smt_auto.task_flow import TaskFlow

    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch, inbound=host.inbound)
    async with host.factory.begin() as session:
        await host.inbound.advance_report(session, plate_id=plate.id, now_ms=2020)
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        assert await host.inbound.bin_closed(session, work)

    async with host.factory() as session:
        final = await session.get(AutoInboundDecisionRound, final_round_id)
        final.previous_round_id = None
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=2030)
            == "WAITING"
        )
        work = await session.get(AutoBinWork, host.work_id)
        assert work.release_command_code is None
        await session.rollback()

    async with host.factory.begin() as session:
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=2030)
            == "RELEASING"
        )
        work = await session.get(AutoBinWork, host.work_id)
        release_code = work.release_command_code
    assert (await host.result(release_code)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=2040)
            == "RELEASED"
        )


async def test_inbound_work_plan_wait_refreshes_only_source_scope(inbound_host):
    host = inbound_host
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        first = await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=20
        )
        original = first.request_data
    await host.settle("putaway.work_plan@v1", {"result": "WAIT", "retry_after_ms": 10})
    async with host.factory.begin() as session:
        first = await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=30
        )
        member = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "other")
        )
        member.withdrawn_at_ms = 31
        before_due = await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=39
        )
        assert before_due.id == first.id
        retry = await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=40
        )
        assert [item["rack_id"] for item in original["source_racks"]] == ["rack", "other"]
        assert first.request_data == original
        assert retry.previous_round_id == first.id
        assert retry.request_data == {**original, "source_racks": original["source_racks"][:1]}
        assert set(retry.source_rack_bindings) == {"rack"}


@pytest.mark.parametrize(
    "wait",
    [
        {"result": "WAIT", "retry_after_ms": True},
        {"result": "WAIT", "retry_after_ms": 0},
        {"result": "WAIT", "retry_after_ms": 10, "target": {}},
    ],
)
async def test_invalid_material_wait_stays_original_and_unresolved(ready_inbound, wait):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        pick_code = plate.pick_command_code
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    await host.settle("putaway.material.decide@v1", wait)
    async with host.factory.begin() as session:
        for now_ms in (60, 100000):
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now_ms
            )
            assert plate.put_command_code is None
        (original,) = (
            await session.scalars(
                select(AutoInboundDecisionRound).where(
                    AutoInboundDecisionRound.plate_id == host.plate_id
                )
            )
        ).all()
        assert original.unresolved
        assert original.wait_recorded_at_ms is None
        assert original.next_eligible_at_ms is None


@pytest.mark.parametrize("decision", ["WAIT", "ACCEPT"])
async def test_broken_material_predecessor_cannot_advance(ready_inbound, decision):
    host = ready_inbound
    operation = "putaway.material.decide@v1"

    async def advance(session, now_ms):
        return await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=now_ms
        )

    async with host.factory.begin() as session:
        pick_code = (await advance(session, 40)).pick_command_code
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await advance(session, 50)
    await host.settle(operation, {"result": "WAIT", "retry_after_ms": 10})
    async with host.factory.begin() as session:
        await advance(session, 60)
        await advance(session, 70)

    if decision == "WAIT":
        await host.settle(operation, {"result": "WAIT", "retry_after_ms": 10})
        async with host.factory.begin() as session:
            await advance(session, 80)
    else:
        await host.settle(
            operation,
            {
                "result": "ACCEPT",
                "target": dict(
                    bin_code="A000000001",
                    cell_id="DEST",
                    bin_type=3,
                    cell_index=1,
                    reel_layer=0,
                    reel_thickness=20,
                    reel_diameter=15,
                    reel_totalthickness=0,
                ),
            },
        )
    async with host.factory.begin() as session:
        successor = await session.scalar(
            select(AutoInboundDecisionRound).where(
                AutoInboundDecisionRound.plate_id == host.plate_id,
                AutoInboundDecisionRound.round_no == 2,
            )
        )
        successor.previous_round_id = None
        outbox_count = await session.scalar(select(func.count()).select_from(ReliableMessage))
        plate = await advance(session, 90)
        assert plate.final_decision_round_id is None
        assert plate.put_command_code is None
        assert (
            await session.scalar(select(func.count()).select_from(ReliableMessage)) == outbox_count
        )


async def test_empty_wait_retry_keeps_original_observation_and_single_new_pick(ready_inbound):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        original_code = plate.pick_command_code
    assert (await host.arm_result(original_code, status="FAILED", empty=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
        original = await session.scalar(
            select(AutoInboundDecisionRound).where(
                AutoInboundDecisionRound.purpose == "EMPTY_DECIDE"
            )
        )
        assert original is not None
        frozen = original.request_data
        assert frozen == dict(
            task_id="t",
            source=dict(rack_id="rack", bin_code="SOURCE", cell_id="CELL"),
            observed_at=99,
        )
    await host.settle("putaway.source.empty_decide@v1", dict(result="WAIT", retry_after_ms=10))
    for now in (60, 69, 70, 70):
        async with host.factory.begin() as session:
            await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=now)
            rounds = list(
                await session.scalars(
                    select(AutoInboundDecisionRound).where(
                        AutoInboundDecisionRound.purpose == "EMPTY_DECIDE"
                    )
                )
            )
            assert len(rounds) == (1 if now < 70 else 2)
            assert all(r.request_data == frozen for r in rounds)
    await host.settle("putaway.source.empty_decide@v1", dict(result="RETRY"))
    for _ in range(2):
        async with host.factory.begin() as session:
            await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=80)
            retry = await session.scalar(
                select(AutoInboundPlate).where(AutoInboundPlate.grant_kind == "RETRY")
            )
            assert retry is not None
            await host.inbound.advance(session, work_line_id=1, plate_id=retry.id, now_ms=81)
            assert retry.pick_command_code != original_code
            retry_id, retry_code = retry.id, retry.pick_command_code
            assert await session.scalar(select(func.count()).select_from(AutoInboundPlate)) == 2
    assert (await host.arm_result(retry_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=retry_id, now_ms=90)
        assert await session.scalar(
            select(AutoInboundDecisionRound).where(
                AutoInboundDecisionRound.purpose == "MATERIAL_DECIDE"
            )
        )


@pytest.mark.parametrize("continuation", ["valid_wait", "conflict", "conflict_after_wait"])
async def test_ng_success_has_no_report_and_original_bin_continuation(ready_inbound, continuation):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        code = plate.pick_command_code
    assert (await host.arm_result(code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    await host.settle(
        "putaway.material.decide@v1",
        dict(
            result="REJECT",
            business_exception_code="MATERIAL_REJECTED",
            ng_locator=dict(type="NG_ZONE", zone_code="STATION_NG"),
            source_disposition="CLOSE",
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code
        code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND", IntegrationExchange.command_code == code
            )
        )
        assert json.loads(await read_history_body(session, (command).request_record_id))[
            "params"
        ] == dict(
            source=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
            target=dict(location_id="STATION_NG", location_type="SCAN_NG"),
        )
    assert (await host.arm_result(code)).status_code == 200
    for _ in range(2):
        async with host.factory.begin() as session:
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=70
            )
            assert plate.report_message_id is None
            assert await host.inbound.closed(session, plate)
            rounds = list(
                await session.scalars(
                    select(AutoInboundDecisionRound).where(
                        AutoInboundDecisionRound.purpose == "WORK_PLAN"
                    )
                )
            )
            assert len(rounds) == 2
            assert rounds[-1].trigger_plate_id == plate.id
            work = await session.get(AutoBinWork, host.work_id)
            assert work.terminal_basis is None

    if continuation != "conflict":
        await host.settle("putaway.work_plan@v1", dict(result="WAIT", retry_after_ms=1))
        async with host.factory.begin() as session:
            await host.work_plans.consume_bin(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=80
            )
            await host.work_plans.start_bin(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=81
            )
    if continuation != "valid_wait":
        conflict = await host.arm_result(code, status="FAILED", empty="PUT_FAILED")
        assert conflict.status_code == 409
    source = {**plate.source_locator, **plate.source_params, "cell_id": "CELL2", "cell_index": 2}
    await host.settle("putaway.work_plan@v1", dict(result="READY", source=source))
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=90
        )
        successor = await session.scalar(
            select(AutoInboundPlate).where(AutoInboundPlate.id != host.plate_id)
        )
        assert successor is not None
        await host.inbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=91)
        assert (successor.pick_command_code is not None) is (continuation == "valid_wait")


@pytest.mark.parametrize("exhausted", [False, True])
async def test_source_absent_continues_original_bin_and_only_explicit_exhaustion(
    ready_inbound, exhausted
):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        code = plate.pick_command_code
    assert (await host.arm_result(code, status="FAILED", empty=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    decision = dict(result="SOURCE_ABSENT")
    if exhausted:
        decision["source_rack_exhausted"] = True
    await host.settle("putaway.source.empty_decide@v1", decision)
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.report_message_id is None
        assert await host.inbound.closed(session, plate)
        assert (
            await host.work_plans.inbound_source_exhausted(
                session,
                task_id="t",
                job_id=plate.source_rack_job_id,
                preparation_action_id=plate.source_preparation_action_id,
            )
            is exhausted
        )
        work = await session.get(AutoBinWork, host.work_id)
        assert not await host.inbound.bin_closed(session, work)
    await host.settle("putaway.work_plan@v1", dict(result="NO_WORK"))
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=70
        )
        assert await host.inbound.bin_closed(session, work)


@pytest.mark.parametrize("outcome", ["NG", "SOURCE_ABSENT"])
@pytest.mark.parametrize("gate", ["disabled", "stale_generation"])
async def test_original_exception_continues_after_current_gate_returns(
    ready_inbound, outcome, gate
):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        pick_code = plate.pick_command_code
    if outcome == "NG":
        assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    else:
        assert (await host.arm_result(pick_code, status="FAILED", empty=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
    if outcome == "NG":
        await host.settle(
            "putaway.material.decide@v1",
            dict(
                result="REJECT",
                business_exception_code="MATERIAL_REJECTED",
                ng_locator=dict(type="NG_ZONE", zone_code="STATION_NG"),
                source_disposition="CLOSE",
            ),
        )
        async with host.factory.begin() as session:
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=60
            )
            put_code = plate.put_command_code
        assert (await host.arm_result(put_code)).status_code == 200
    else:
        put_code = None
        await host.settle("putaway.source.empty_decide@v1", dict(result="SOURCE_ABSENT"))
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        original_version = line.version
        if gate == "disabled":
            line.is_enabled = False
        else:
            line.version += 1
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=70
        )
        assert await host.inbound.closed(session, plate)
        assert plate.pending
        assert not await session.scalar(
            select(AutoInboundDecisionRound.id).where(
                AutoInboundDecisionRound.trigger_plate_id == plate.id
            )
        )
        line = await session.get(WorkLine, 1)
        line.is_enabled, line.version = True, original_version
    for now in (80, 90):
        async with host.factory.begin() as session:
            plate = await host.inbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now
            )
            assert await host.inbound.closed(session, plate)
            assert not plate.pending
            assert (plate.pick_command_code, plate.put_command_code) == (pick_code, put_code)
            rounds = list(
                await session.scalars(
                    select(AutoInboundDecisionRound).where(
                        AutoInboundDecisionRound.trigger_plate_id == plate.id
                    )
                )
            )
            assert len(rounds) == 1
            work = await session.get(AutoBinWork, host.work_id)
            assert rounds[0].request_data["scanned_at"] == work.scanned_at


async def test_exhausted_source_handoff_does_not_wait_for_independent_put_report(ready_inbound):
    host = ready_inbound
    await decide_exhaustion(host)
    async with host.factory.begin() as session:
        plate = await session.get(AutoInboundPlate, host.plate_id)
        demand, handoff = await AutoRackPorts(host.dispatch, inbound=host.inbound).facts(
            session, job_id=plate.source_rack_job_id
        )
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"


async def test_same_rack_new_plan_member_reuses_original_admission(ready_inbound):
    from app.businesses.smt_auto.source_flow import SourceFlow

    host = ready_inbound
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "t")
        task.plan_revision = 2
        plan = AutoTaskInput(
            task_id="t",
            operation="sorting.task.plan_delta@v1",
            operation_id="plan2",
            request_timestamp=2,
            first_received_at_ms=2,
            effective_digest="d2",
            contract_version="v1",
            disposition="APPLIED",
            applied_plan_revision=2,
            effective_request={"added_source_racks": [{"rack_id": "rack"}]},
        )
        session.add(plan)
        await session.flush()
        original = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "rack")
        )
        member = AutoInboundSourceMember(
            task_id="t", plan_input_id=plan.id, plan_revision=2, rack_id="rack", rack_array_index=0
        )
        session.add(member)
        await session.flush()
        flow = SourceFlow(host.inbound, station_id="station")
        await flow.advance(session, task_id="t", now_ms=50)
        assert member.rack_job_id == original.rack_job_id
        assert member.preparation_action_id == original.preparation_action_id
        original.withdrawn_at_ms = 51
        bindings = await host.work_plans._lock_line(session, 1)
        sources, proofs = await host.work_plans._inbound_sources(session, task, bindings)
        assert {"rack_id": "rack", "location_code": "P"} in sources
        assert proofs["rack"]["preparation_action_id"] == original.preparation_action_id


async def test_initial_empty_notification_uses_original_transport_and_waits_for_arrival(
    inbound_host,
):
    from app.businesses.smt_auto.inbound_contracts import RackExhausted
    from app.integrations.wms_callback import CallbackContext

    host = inbound_host
    async with host.factory.begin() as session:
        member = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "rack")
        )
        progress = await host.dispatch.progress(session, job_id=member.rack_job_id)
        action = next(a for a in progress.actions if a.id == member.preparation_action_id)
        payload = RackExhausted(
            task_id="t", rack_id="rack", transport_task_id=action.transport_task_id
        )
        ctx = CallbackContext(
            session, 1, "empty-notice", 55, {}, operation="putaway.source.rack_exhausted@v1"
        )
        await host.inbound.accept_source_rack_exhausted(ctx, payload)
        await host.inbound.accept_source_rack_exhausted(ctx, payload)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AutoTaskInput)
                .where(AutoTaskInput.operation == ctx.operation)
            )
            == 1
        )
        assert await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=member.rack_job_id,
            preparation_action_id=member.preparation_action_id,
        )
        assert not await host.work_plans.inbound_source_exhausted(
            session, task_id="t", job_id=member.rack_job_id, preparation_action_id="new-arrival"
        )


@pytest.mark.parametrize("inbound_host", [("ARM02", "ARM01", "new")], indirect=True)
async def test_source_candidates_prepare_left_then_right_without_claiming_arrival(inbound_host):
    from app.businesses.smt_auto.source_flow import SourceFlow
    from app.integrations.wms_transport import TransportCaller

    host = inbound_host
    flow = SourceFlow(host.inbound, station_id="station")
    async with host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=40)
        members = list(
            await session.scalars(
                select(AutoInboundSourceMember).order_by(AutoInboundSourceMember.rack_array_index)
            )
        )
        assert all(m.rack_job_id and m.preparation_action_id for m in members)
        jobs = [await host.dispatch.progress(session, job_id=m.rack_job_id) for m in members]
        assert [j.origin.point for j in jobs] == ["P", "Q"]
        assert all(j.origin.caller == TransportCaller("1", "station") for j in jobs)
        assert all(len(j.actions) == 1 and j.actions[0].kind == "ENTER" for j in jobs)
        task = await session.get(AutoTask, "t")
        bindings = await host.work_plans._lock_line(session, 1)
        assert await host.work_plans._inbound_sources(session, task, bindings) == ([], {})
        from app.businesses.smt_auto.inbound_contracts import RackExhausted
        from app.integrations.wms_callback import CallbackContext

        member = members[0]
        action = jobs[0].actions[0]
        ctx = CallbackContext(
            session, 1, "early-empty", 40, {}, operation="putaway.source.rack_exhausted@v1"
        )
        await host.inbound.accept_source_rack_exhausted(
            ctx,
            RackExhausted(
                task_id="t", rack_id=member.rack_id, transport_task_id=action.transport_task_id
            ),
        )
        assert not await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=member.rack_job_id,
            preparation_action_id=member.preparation_action_id,
        )
        await apply_outcome(session, action, "SUCCEEDED", point="P", rack_id="rack")
        await flow.advance(session, task_id="t", now_ms=41)
        assert await host.work_plans.inbound_source_exhausted(
            session,
            task_id="t",
            job_id=member.rack_job_id,
            preparation_action_id=member.preparation_action_id,
        )
        await flow.advance(session, task_id="t", now_ms=41)
        assert (
            len(
                await host.dispatch.jobs(
                    session, work_line_id=1, source_kinds=("AUTO_INBOUND_SOURCE",)
                )
            )
            == 2
        )
        from tests.businesses.rack_dispatch import respond

        job_id = member.rack_job_id
        root = (await host.dispatch.progress(session, job_id=job_id)).actions[-1]
        assert root.kind == "DEPARTURE_DECIDE"
        await respond(
            session,
            root.decision_operation_id,
            200,
            "DECIDED",
            dict(result="WAIT", retry_after_ms=10),
        )
        await flow.advance(session, task_id="t", now_ms=50)
        await flow.advance(session, task_id="t", now_ms=60)
        successor = (await host.dispatch.progress(session, job_id=job_id)).actions[-1]
        assert successor.id != root.id and successor.previous_action_id == root.id
        await respond(
            session,
            successor.decision_operation_id,
            200,
            "DECIDED",
            dict(result="READY", rack_destination=dict(type="ZONE", location_code="Z")),
        )
        await flow.advance(session, task_id="t", now_ms=61)
        depart = (await host.dispatch.progress(session, job_id=job_id)).actions[-1]
        assert depart.kind == "DEPART" and depart.template == "F01"
        await respond(
            session,
            depart.submit_operation_id,
            202,
            "RECEIVED",
            dict(transport_task_id=depart.transport_task_id),
        )
        await flow.advance(session, task_id="t", now_ms=62)
        assert (await host.dispatch.progress(session, job_id=job_id)).state == "COMPLETED"


@pytest.mark.parametrize(
    "status,stage", [("SUCCESS", True), ("FAILED", "PICK_FAILED"), ("FAILED", "PUT_FAILED")]
)
async def test_nonempty_outcome_does_not_authorize_empty_decision(ready_inbound, status, stage):
    host = ready_inbound
    async with host.factory.begin() as session:
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        code = plate.pick_command_code
    assert (await host.arm_result(code, status=status, empty=stage)).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=50)
        assert not await session.scalar(
            select(AutoInboundDecisionRound.id).where(
                AutoInboundDecisionRound.purpose == "EMPTY_DECIDE"
            )
        )


@pytest.mark.parametrize("exception", ["NG", "SOURCE_ABSENT"])
@pytest.mark.parametrize("broken_grant", [False, True])
async def test_consumed_next_allows_exception_continuation_before_parent_report(
    ready_inbound, exception, broken_grant
):
    host = ready_inbound
    async with host.factory.begin() as session:
        parent = await session.get(AutoInboundPlate, host.plate_id)
        source = {
            **parent.source_locator,
            **parent.source_params,
            "rack_id": "other",
            "location_code": "Q",
        }
    await decide_exhaustion(host, next_source=source)
    async with host.factory.begin() as session:
        parent = await session.get(AutoInboundPlate, host.plate_id)
        put_code = parent.put_command_code
        child = await session.scalar(
            select(AutoInboundPlate).where(AutoInboundPlate.grant_parent_plate_id == parent.id)
        )
        child_id = child.id
        await host.inbound.advance(session, work_line_id=1, plate_id=child_id, now_ms=61)
        pick_code = child.pick_command_code
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        parent = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=62
        )
        assert parent.report_message_id is not None and parent.report_consumed_at_ms is None
    assert (
        await host.arm_result(
            pick_code,
            scan=exception == "NG",
            status="SUCCESS" if exception == "NG" else "FAILED",
            empty=exception == "SOURCE_ABSENT",
        )
    ).status_code == 200
    async with host.factory.begin() as session:
        await host.inbound.advance(session, work_line_id=1, plate_id=child_id, now_ms=63)
    if exception == "NG":
        await host.settle(
            "putaway.material.decide@v1",
            dict(
                result="REJECT",
                business_exception_code="MATERIAL_REJECTED",
                ng_locator=dict(type="NG_ZONE", zone_code="STATION_NG"),
                source_disposition="CLOSE",
            ),
        )
        async with host.factory.begin() as session:
            child = await host.inbound.advance(
                session, work_line_id=1, plate_id=child_id, now_ms=64
            )
            ng_code = child.put_command_code
        assert (await host.arm_result(ng_code)).status_code == 200
    else:
        await host.settle("putaway.source.empty_decide@v1", dict(result="SOURCE_ABSENT"))
    for _ in range(2):
        async with host.factory.begin() as session:
            child = await session.get(AutoInboundPlate, child_id)
            if broken_grant:
                child.grant_snapshot = {}
            await host.inbound.advance(session, work_line_id=1, plate_id=child_id, now_ms=65)
            continuations = list(
                await session.scalars(
                    select(AutoInboundDecisionRound).where(
                        AutoInboundDecisionRound.trigger_plate_id == child_id
                    )
                )
            )
            assert len(continuations) == (0 if broken_grant else 1)
            parent = await session.get(AutoInboundPlate, host.plate_id)
            assert parent.report_consumed_at_ms is None
            if continuations:
                work = await session.get(AutoBinWork, host.work_id)
                assert continuations[0].request_data["scanned_at"] == work.scanned_at


async def test_original_ready_can_pick_after_last_source_member_withdrawal(ready_inbound):
    from app.businesses._components.sorting_task.contracts import Cancel
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    host = ready_inbound
    async with host.factory.begin() as session:
        member = await session.scalar(
            select(AutoInboundSourceMember).where(AutoInboundSourceMember.rack_id == "rack")
        )
        payload = Cancel.model_validate(
            {
                "task_id": "t",
                "cancel_scope": "PLAN_MEMBERS",
                "source_racks": [
                    {"plan_revision": member.plan_revision, "rack_id": member.rack_id}
                ],
            }
        )
        ctx = CallbackContext(
            session, 1, "cancel-original-ready", 35, {}, operation="sorting.task.cancel@v1"
        )
        await AutoTaskService().accept_task_cancel(ctx, payload)
        assert member.withdrawn_at_ms == 35
        plate = await host.inbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=40
        )
        assert plate.pick_command_code
        assert not await host.inbound.source_clear(session, job_id=member.rack_job_id)
