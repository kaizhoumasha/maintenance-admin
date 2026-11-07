"""Original SCAN2 work plans use the public WMS outbox and preserve one identity."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import AutoBinWork, AutoTask, AutoTaskRequest
from app.integrations import history as history_records
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import (
    Device,
    DeviceRoute,
    ReliableMessage,
    WorkLine,
    WorkLinePosition,
)
from tests.helpers import read_history_body


async def test_repeated_bin_entry_preserves_original_outbox_and_subject(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    service = WorkPlans(direction_host.requests)
    async with direction_host.factory.begin() as session:
        first = await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=20)
        second = await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=99)
        assert first.id == second.id
        assert first.request_data == {"task_id": "t", "bin_code": "BIN", "scanned_at": 10}
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        assert first.unresolved


@pytest.mark.parametrize("reason", ["CONTEXT_NOT_READY", "CONFIRMATION_PENDING", "INVALID_SCAN"])
async def test_held_original_arrival_never_creates_first_request(direction_host, reason):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        row = await session.scalar(select(AutoBinWork))
        row.production_hold_reason = reason
        assert (
            await WorkPlans(direction_host.requests).start_bin(
                session, work_line_id=1, arrival_id="arrival", now_ms=20
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def settle(host, data, *, status=200, code="DECIDED"):
    import json

    from app.http_client import DeliveryState, HttpResult
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus

    store = MessageStore(host.factory, records=history_records)
    claims = await store.claim(1, host.requests.outbox.targets)
    assert len(claims) == 1
    claim = claims[0]
    response = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        status,
        (),
        json.dumps(
            {
                "operation_id": claim.message.operation_id,
                "code": code,
                "timestamp": 30,
                "data": data,
            }
        ).encode(),
    )
    verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
    assert await store.finish(
        claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    )


@pytest.mark.parametrize(
    "decision",
    [
        {
            "result": "READY",
            "cell_id": "BIN3",
            "cell_index": 3,
            "bin_type": 3,
            "reel_layer": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
        },
        {"result": "NO_WORK"},
        {"result": "WAIT", "retry_after_ms": 1000},
    ],
)
async def test_original_decision_is_consumed_once_without_physical_completion(
    direction_host, decision
):
    from app.businesses.smt_auto.models import AutoOutboundPlate
    from app.businesses.smt_auto.work_plans import WorkPlans

    service = WorkPlans(direction_host.requests)
    async with direction_host.factory.begin() as session:
        original = await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=20)
        original_id = original.id
    await settle(direction_host, decision)
    async with direction_host.factory.begin() as session:
        result = await service.consume_bin(session, work_line_id=1, arrival_id="arrival", now_ms=40)
        assert result.id == original_id and not result.unresolved
        await service.consume_bin(session, work_line_id=1, arrival_id="arrival", now_ms=90)
        subject = await session.scalar(select(AutoBinWork))
        assert subject.release_command_code is None
        assert await session.scalar(select(func.count()).select_from(AutoOutboundPlate)) == (
            decision["result"] == "READY"
        )
        if decision["result"] == "NO_WORK":
            assert subject.terminal_basis is not None
        elif decision["result"] == "WAIT":
            assert result.next_eligible_at_ms == 1040
            assert (
                await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=1039)
            ).id == original_id
            successor = await service.start_bin(
                session, work_line_id=1, arrival_id="arrival", now_ms=1040
            )
            assert successor.id != original_id and successor.previous_round_id == original_id
            assert successor.request_data == result.request_data


@pytest.mark.parametrize(
    "decision",
    [
        {"result": "NO_WORK", "retry_after_ms": 1},
        {"result": "WAIT", "retry_after_ms": True},
        {
            "result": "READY",
            "cell_id": "BIN3",
            "cell_index": 3,
            "bin_type": 3,
            "reel_layer": 1,
            "reel_thickness": None,
            "reel_diameter": 15,
        },
    ],
)
async def test_invalid_decision_leaves_original_pending(direction_host, decision):
    from app.businesses.smt_auto.work_plans import WorkPlans

    service = WorkPlans(direction_host.requests)
    async with direction_host.factory.begin() as session:
        await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=20)
    await settle(direction_host, decision)
    async with direction_host.factory.begin() as session:
        result = await service.consume_bin(session, work_line_id=1, arrival_id="arrival", now_ms=40)
        assert result.unresolved and result.consumed_at_ms is None


async def test_scan2_delegates_first_intent_in_same_transaction(direction_host):
    from app.businesses._components.bin_roller.ports import Scan2Arrival
    from app.businesses.smt_auto.roller_ports import RollerPorts
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        ports = RollerPorts(work_plans=WorkPlans(direction_host.requests))
        await ports.accept_scan2(
            SimpleNamespace(session=session),
            Scan2Arrival("new", 1, "SCAN2", "SECOND-C", "SECOND", 11),
        )
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1


async def test_return_face_without_original_success_never_requests(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        assert (
            await WorkPlans(direction_host.requests).start_return_face(
                session, work_line_id=1, member_id=999, now_ms=20
            )
            is None
        )


async def source_face(host, session, *, inbound=False, success=True, advance=True):
    from dataclasses import asdict, replace

    from app.businesses.smt_auto.models import (
        AutoInboundSourceMember,
        AutoOutboundSourceMember,
        AutoTaskInput,
    )
    from app.businesses.smt_auto.rack_ports import AutoRackPorts, RackAuthorization
    from app.integrations.ecs_callback.binding import DeviceBinding
    from tests.businesses.rack_dispatch import apply_outcome, origin

    bin_ = await session.scalar(select(AutoBinWork))
    bin_values = {
        column.name: getattr(bin_, column.name) for column in AutoBinWork.__table__.columns
    }
    await session.delete(bin_)
    await session.flush()
    task = await session.get(AutoTask, "t")
    task.business_type = "inbound" if inbound else "outbound"
    task.plan_revision = 1
    line = await session.get(WorkLine, 1)
    line.device_bindings = {"EXPLICIT_TEST_ROLE": "ARM"}
    line.position_bindings = {"RETURN_RACK": "P"}
    line.version = 1
    session.add(WorkLinePosition(work_line_id=1, position_code="P", capacity=None))
    session.add(DeviceRoute(id="route", device_code="ARM", ecs_origin="http://example.test"))
    await session.flush()
    session.add(
        Device(
            id=1,
            device_code="ARM",
            display_name="ARM",
            work_line_id=1,
            current_route_id="route",
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
        effective_request={"added_source_racks": [{"rack_id": "rack"}]}
        if inbound
        else {
            "target_rack": {"rack_id": "target", "rack_face": "90"},
            "added_return_racks": [{"rack_id": "rack", "rack_faces": ["90"]}],
        },
        disposition="APPLIED",
        applied_plan_revision=1,
    )
    session.add(plan)
    await session.flush()
    model = AutoInboundSourceMember if inbound else AutoOutboundSourceMember
    member = model(
        task_id="t",
        plan_input_id=plan.id,
        plan_revision=1,
        rack_id="rack",
        rack_array_index=0,
        **({} if inbound else {"rack_face": "90"}),
    )
    session.add(member)
    await session.flush()
    binding = DeviceBinding(
        device_id=1,
        device_code="ARM",
        device_version=1,
        work_line_id=1,
        work_line_version=1,
        device_role="EXPLICIT_TEST_ROLE",
        device_bindings=dict(line.device_bindings),
        position_bindings=dict(line.position_bindings),
        business_code="smt_auto",
    )
    auth = RackAuthorization("INBOUND_MEMBER" if inbound else "OUTBOUND_MEMBER", member.id)
    original = replace(
        origin("AUTO_INBOUND_SOURCE" if inbound else "AUTO_OUTBOUND_RETURN"),
        work_line_id=1,
        task_id="t",
        source_ref=auth.source_ref,
        initial_face=None if inbound else "90",
        resources=asdict(binding),
    )
    ports = AutoRackPorts(host.dispatch)
    job = await ports.register(session, original, authorization=auth, original_binding=binding)
    assert job
    action = await ports.prepare(
        session,
        job_id=job,
        kind="ENTER",
        authorization=auth,
        face=None if inbound else "90",
        original_binding=binding,
    )
    assert action
    progress = await host.dispatch.progress(session, job_id=job)
    await apply_outcome(session, progress.actions[-1], "SUCCEEDED" if success else "FAILED")
    if advance:
        await ports.advance(session, job_id=job, now_ms=10)
    session.add(AutoBinWork(**bin_values))
    await session.flush()
    return member


@pytest.mark.parametrize("inbound", [False, True])
async def test_real_source_success_binds_original_work_plan(direction_host, inbound):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session, inbound=inbound)
        service = WorkPlans(direction_host.requests, direction_host.dispatch)
        if inbound:
            subject = await session.scalar(select(AutoBinWork))
            subject.business_type = "inbound"
            round_ = await service.start_bin(
                session, work_line_id=1, arrival_id="arrival", now_ms=20
            )
            assert round_.request_data["source_racks"] == [
                {"rack_id": "rack", "location_code": "P"}
            ]
            assert (
                round_.source_rack_bindings["rack"]["preparation_action_id"]
                == member.preparation_action_id
            )
        else:
            round_ = await service.start_return_face(
                session, work_line_id=1, member_id=member.id, now_ms=20
            )
            assert round_ is not None
            assert member.first_work_plan_round_id == round_.id
            assert round_.request_data == {
                "task_id": "t",
                "rack_id": "rack",
                "rack_face": "90",
                "arrived_at": 10,
            }


async def test_failed_return_source_does_not_start_face(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session, success=False)
        assert (
            await WorkPlans(direction_host.requests, direction_host.dispatch).start_return_face(
                session, work_line_id=1, member_id=member.id, now_ms=20
            )
            is None
        )
        assert member.first_work_plan_round_id is None


@pytest.mark.parametrize(
    "decision",
    [
        {
            "result": "READY",
            "rack_id": "rack",
            "rack_face": "90",
            "slot_id": "S3",
            "rack_layer": 2,
            "rack_column": 3,
            "reel_thickness": 15.3,
            "reel_diameter": 7,
        },
        {"result": "NO_WORK"},
        {"result": "WAIT", "retry_after_ms": 1000},
        {
            "result": "READY",
            "rack_id": "wrong",
            "rack_face": "90",
            "slot_id": "S3",
            "rack_layer": 2,
            "rack_column": 3,
            "reel_thickness": 15.3,
            "reel_diameter": 7,
        },
    ],
)
async def test_return_face_consumes_only_exact_original_grant(direction_host, decision):
    from app.businesses.smt_auto.models import AutoOutboundPlate, AutoOutboundSourceMember
    from app.businesses.smt_auto.work_plans import WorkPlans

    service = WorkPlans(direction_host.requests, direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session)
        member_id = member.id
        original = await service.start_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=20
        )
        original_id = original.id
    await settle(direction_host, decision)
    async with direction_host.factory.begin() as session:
        result = await service.consume_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=40
        )
        invalid = decision.get("rack_id") == "wrong"
        assert result.unresolved == invalid
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert member.first_work_plan_round_id == original_id
        assert await session.scalar(select(func.count()).select_from(AutoOutboundPlate)) == (
            decision["result"] == "READY" and not invalid
        )
        if decision["result"] == "WAIT":
            successor = await service.start_return_face(
                session, work_line_id=1, member_id=member_id, now_ms=1040
            )
            assert successor.request_data == result.request_data
            assert successor.previous_round_id == result.id
        elif decision["result"] == "NO_WORK":
            assert member.terminal_basis["round_id"] == result.id
            assert (
                await service.start_return_face(
                    session, work_line_id=1, member_id=member_id, now_ms=2000
                )
                is None
            )


async def test_started_face_cancellation_preserves_original_member(direction_host):
    from app.businesses._components.sorting_task.contracts import Cancel
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session)
        await WorkPlans(direction_host.requests, direction_host.dispatch).start_return_face(
            session, work_line_id=1, member_id=member.id, now_ms=20
        )
        ctx = SimpleNamespace(
            session=session,
            operation="sorting.task.cancel@v1",
            operation_id="cancel",
            timestamp=30,
            parsed={},
        )
        await AutoTaskService().accept_task_cancel(
            ctx,
            Cancel(
                task_id="t",
                cancel_scope="PLAN_MEMBERS",
                direct_pick_sources=[{"plan_revision": 1, "rack_id": "rack", "rack_face": "90"}],
            ),
        )
        assert member.withdrawn_by_input_id is None


@pytest.mark.parametrize("status,code", [(503, "UNAVAILABLE"), (200, "UNRECOGNIZED")])
async def test_unknown_response_recovers_same_frozen_first_intent(direction_host, status, code):
    import json

    from app.businesses.smt_auto.work_plans import WorkPlans
    from app.http_client import DeliveryState, HttpResult
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus

    service = WorkPlans(direction_host.requests)
    async with direction_host.factory.begin() as session:
        original = await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=20)
        original_id, original_operation = original.id, original.operation_id
    store = MessageStore(direction_host.factory, records=history_records)
    (claim,) = await store.claim(1, direction_host.requests.outbox.targets)
    frozen = claim.message.request_raw
    response = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        status,
        (),
        json.dumps(
            {"operation_id": original_operation, "code": code, "timestamp": 30, "data": {}}
        ).encode(),
    )
    verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
    await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.PENDING,
            result=response,
            verdict=verdict,
            next_attempt_at=direction_host.requests.clock(),
        ),
    )
    async with direction_host.factory.begin() as session:
        result = await service.consume_bin(session, work_line_id=1, arrival_id="arrival", now_ms=40)
        assert result.unresolved
        repeated = await service.start_bin(
            session, work_line_id=1, arrival_id="arrival", now_ms=1000
        )
        assert (repeated.id, repeated.operation_id) == (original_id, original_operation)
        message = await session.get(ReliableMessage, repeated.message_id)
        assert await read_history_body(session, (message).request_record_id) == frozen


async def test_first_intent_rolls_back_with_original_caller_transaction(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory() as session:
        await WorkPlans(direction_host.requests).start_bin(
            session, work_line_id=1, arrival_id="arrival", now_ms=20
        )
        await session.rollback()
    async with direction_host.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


@pytest.mark.parametrize("rack", ["rack", "wrong"])
async def test_inbound_ready_uses_frozen_original_source(direction_host, rack):
    from app.businesses.smt_auto.models import AutoInboundPlate
    from app.businesses.smt_auto.work_plans import WorkPlans

    service = WorkPlans(direction_host.requests, direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session, inbound=True)
        subject = await session.scalar(select(AutoBinWork))
        subject.business_type = "inbound"
        await service.start_bin(session, work_line_id=1, arrival_id="arrival", now_ms=20)
        job, action = member.rack_job_id, member.preparation_action_id
    await settle(
        direction_host,
        {
            "result": "READY",
            "source": {
                "rack_id": rack,
                "location_code": "P",
                "bin_code": "SOURCE",
                "bin_location": "A",
                "cell_id": "SOURCE1",
                "bin_type": 3,
                "cell_index": 1,
                "reel_layer": 1,
                "reel_thickness": 20,
                "reel_diameter": 15,
                "reel_totalthickness": 20,
            },
        },
    )
    async with direction_host.factory.begin() as session:
        result = await service.consume_bin(session, work_line_id=1, arrival_id="arrival", now_ms=40)
        assert result.unresolved == (rack == "wrong")
        plate = await session.scalar(select(AutoInboundPlate))
        if rack == "rack":
            assert (plate.source_rack_job_id, plate.source_preparation_action_id) == (job, action)
        else:
            assert plate is None


async def test_return_arrival_confirmation_survives_admission_hold_restart_and_wait(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session)
        member_id = member.id
        confirmation = AutoTaskRequest(
            task_id="t",
            purpose="COMPLETION",
            operation="sorting.task.completion_confirm@v1",
            operation_id="completion",
            message_id=999,
            request_data={"task_id": "t", "last_applied_plan_revision": 1},
            unresolved=True,
        )
        session.add(confirmation)
        await session.flush()
        service = WorkPlans(direction_host.requests, direction_host.dispatch)
        assert (
            await service.start_return_face(
                session, work_line_id=1, member_id=member_id, now_ms=100
            )
            is None
        )
        await session.delete(confirmation)
    async with direction_host.factory.begin() as session:
        restarted = WorkPlans(direction_host.requests, direction_host.dispatch)
        original = await restarted.start_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=200
        )
        assert original.request_data["arrived_at"] == 10
    await settle(direction_host, {"result": "WAIT", "retry_after_ms": 1000})
    async with direction_host.factory.begin() as session:
        restarted = WorkPlans(direction_host.requests, direction_host.dispatch)
        await restarted.consume_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=400
        )
        successor = await restarted.start_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=1400
        )
        assert successor.request_data["arrived_at"] == 10


async def test_return_missing_original_confirmation_time_blocks_first_request(direction_host):
    from app.businesses.smt_auto.work_plans import WorkPlans

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session)
        member.arrived_at_ms = None
        from app.businesses.smt_auto.rack_ports import AutoRackPorts

        await AutoRackPorts(direction_host.dispatch).advance(
            session, job_id=member.rack_job_id, now_ms=200
        )
        assert member.arrived_at_ms is None
        assert (
            await WorkPlans(direction_host.requests, direction_host.dispatch).start_return_face(
                session, work_line_id=1, member_id=member.id, now_ms=200
            )
            is None
        )


async def test_public_source_advance_locks_line_jobs_subjects_before_actions(direction_host):
    from sqlalchemy import event

    from app.businesses.smt_auto.rack_ports import AutoRackPorts

    engine = direction_host.factory.kw["bind"].sync_engine
    locks = []

    def record(connection, clause, multiparams, params, execution_options):
        if getattr(clause, "_for_update_arg", None) is not None:
            locks.extend(table.name for table in clause.get_final_froms())

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session, advance=False)
        event.listen(engine, "before_execute", record)
        try:
            await AutoRackPorts(direction_host.dispatch).advance(
                session, job_id=member.rack_job_id, now_ms=10
            )
        finally:
            event.remove(engine, "before_execute", record)
        expected = [
            "work_lines",
            "devices",
            "rack_dispatch_job",
            "smt_auto_task",
            "smt_auto_outbound_source_member",
            "rack_dispatch_action",
        ]
        # Assert actual lock-bearing SQL, including empty subject sets, not helper call counts.
        assert all(name in locks for name in expected), locks
        assert [locks.index(name) for name in expected] == sorted(
            locks.index(name) for name in expected
        )
        assert "work_lines" not in locks[locks.index("rack_dispatch_action") + 1 :]
        assert (
            "smt_auto_outbound_source_member"
            not in locks[locks.index("rack_dispatch_action") + 1 :]
        )
        assert member.arrived_at_ms == 10


async def test_changed_admission_job_set_aborts_before_original_fact_consumption(
    direction_host, monkeypatch
):
    from app.businesses._components.bin_roller.service import RackLockSetChanged
    from app.businesses.smt_auto.models import AutoOutboundSourceMember
    from app.businesses.smt_auto.rack_ports import AutoRackPorts

    async with direction_host.factory.begin() as session:
        member = await source_face(direction_host, session, advance=False)
        job_id, member_id = member.rack_job_id, member.id
    read = direction_host.dispatch.admission_jobs
    calls = 0

    async def growing_set(session, *, work_line_id):
        nonlocal calls
        calls += 1
        actual = await read(session, work_line_id=work_line_id)
        # Model a first enumeration that preceded this already real original job.
        return () if calls == 1 else actual

    monkeypatch.setattr(direction_host.dispatch, "admission_jobs", growing_set)
    with pytest.raises(RackLockSetChanged):
        async with direction_host.factory.begin() as session:
            await AutoRackPorts(direction_host.dispatch).advance(session, job_id=job_id, now_ms=10)
    monkeypatch.setattr(direction_host.dispatch, "admission_jobs", read)
    async with direction_host.factory.begin() as session:
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert member.arrived_at_ms is None
        progress = await direction_host.dispatch.progress(session, job_id=job_id)
        assert progress.actions[-1].phase != "APPLIED"
        await AutoRackPorts(direction_host.dispatch).advance(session, job_id=job_id, now_ms=20)
        assert member.arrived_at_ms == 20
