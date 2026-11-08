"""Exercise AUTO facts and current gates through real public RackDispatch/outbox."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, func, select

from app.businesses.smt_auto.models import (
    TABLES,
    AutoBinWork,
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.businesses.smt_auto.rack_ports import AutoRackPorts, RackAuthorization
from app.integrations.ecs_callback.binding import DeviceBinding
from app.models import Device, DeviceRoute, ReliableMessage, WorkLine
from app.models.base import Base
from tests.businesses.rack_dispatch import apply_outcome, origin, respond, store


@pytest.fixture
async def rack_host():
    async with store("sqlite+aiosqlite://", owner="smt_auto") as (factory, dispatch):
        metadata = MetaData(schema=Base.metadata.schema)
        for table in (
            *TABLES.values(),
            Device.__table__,
            DeviceRoute.__table__,
            WorkLine.__table__,
        ):
            table.to_metadata(metadata)
        engine = factory.kw["bind"]
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with factory() as session:
            async with session.begin():
                line = await session.get(WorkLine, 12)
                line.device_bindings = {"EXPLICIT_TEST_ROLE": "ARM"}
                line.version = 1
                session.add(
                    DeviceRoute(id="route", device_code="ARM", ecs_origin="http://example.test")
                )
                await session.flush()
                session.add(
                    Device(
                        id=1,
                        device_code="ARM",
                        display_name="ARM",
                        work_line_id=12,
                        current_route_id="route",
                        version=1,
                    )
                )
                task = AutoTask(
                    task_id="task",
                    business_type="inbound",
                    work_line_id=12,
                    workline_code="LINE",
                    issued_operation_id="issued",
                    issued_timestamp=1,
                    issued_data={},
                    status="EXECUTING",
                    dispatch_sequence=1,
                    plan_revision=1,
                    prepare_accepted_at_ms=1,
                )
                session.add(task)
                await session.flush()
                plan = AutoTaskInput(
                    task_id="task",
                    operation="sorting.task.plan_delta@v1",
                    operation_id="plan",
                    request_timestamp=1,
                    first_received_at_ms=1,
                    effective_digest="d",
                    contract_version="v1",
                    effective_request={"added_source_racks": [{"rack_id": "rack"}]},
                    disposition="APPLIED",
                    applied_plan_revision=1,
                )
                session.add(plan)
                session.add(
                    AutoTaskRequest(
                        task_id="task",
                        purpose="PREPARE",
                        operation="sorting.task.prepare@v1",
                        operation_id="prepare",
                        message_id=500,
                        request_data={"task_id": "task", "workline_code": "LINE"},
                        response_snapshot={
                            "http_status": 202,
                            "code": "PREPARE_ACCEPTED",
                            "data": {},
                        },
                        consumed_at_ms=1,
                        unresolved=False,
                    )
                )
                await session.flush()
                member = AutoInboundSourceMember(
                    task_id="task",
                    plan_input_id=plan.id,
                    plan_revision=1,
                    rack_id="rack",
                    rack_array_index=0,
                )
                session.add(member)
                await session.flush()
            binding = DeviceBinding(
                device_id=1,
                device_code="ARM",
                device_version=1,
                work_line_id=12,
                work_line_version=1,
                device_role="EXPLICIT_TEST_ROLE",
                device_bindings={"EXPLICIT_TEST_ROLE": "ARM"},
                position_bindings={"RETURN_RACK": "P"},
                business_code="smt_auto",
            )
            authorization = RackAuthorization("INBOUND_MEMBER", member.id)
            original = replace(
                origin("AUTO_INBOUND_SOURCE"),
                source_ref=authorization.source_ref,
                initial_face=None,
                resources=asdict(binding),
            )
            ports = AutoRackPorts(dispatch)
            yield session, ports, dispatch, binding, authorization, original, member
            await session.rollback()
        # Only the test's independent copied metadata owns these extra tables.
        async with engine.begin() as connection:
            for table in sorted(
                metadata.tables.values(),
                key=lambda table: (
                    0 if table.name == "devices" else 1 if table.name == "device_routes" else 2,
                    -list(metadata.tables).index(table.key),
                ),
            ):
                if table.name != WorkLine.__tablename__:
                    await connection.run_sync(lambda sync, table=table: table.drop(sync))


async def test_current_member_grant_prepares_atomic_original_intent(rack_host):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    assert job == member.rack_job_id
    demand, handoff = await ports.facts(db, job_id=job)
    assert (demand.state, handoff.state) == ("CONTINUE", "CLEAR")
    assert len(demand.coverage) == 11 and all(c.anchor.key == ("task",) for c in demand.coverage)
    action = await ports.prepare(
        db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
    )
    assert action == member.preparation_action_id
    view = await dispatch.progress(db, job_id=job)
    assert view.actions[0].transport_task_id and view.actions[0].submit_message_id
    assert (
        await ports.prepare(
            db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
        )
        == action
    )


@pytest.mark.parametrize(
    "change",
    ["disabled", "line_version", "device_version", "point", "owner", "task", "rack", "face"],
)
async def test_identity_or_current_binding_drift_never_registers_intent(rack_host, change):
    db, ports, dispatch, binding, auth, original, member = rack_host
    if change == "disabled":
        (await db.get(WorkLine, 12)).is_enabled = False
    elif change == "line_version":
        (await db.get(WorkLine, 12)).version = 2
    elif change == "device_version":
        (await db.get(Device, 1)).version = 2
    elif change == "owner":
        (await db.get(WorkLine, 12)).business_code = "other"
    else:
        field = {"point": "point", "task": "task_id", "rack": "rack_id", "face": "initial_face"}[
            change
        ]
        original = replace(original, **{field: "OTHER"})
    await db.flush()
    assert await ports.register(db, original, authorization=auth, original_binding=binding) is None
    assert await dispatch.pending_upper_id(db, source_kinds=("AUTO_INBOUND_SOURCE",)) is None


async def test_withdrawal_blocks_new_prepare_without_swallowing_original_recovery(rack_host):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    member.withdrawn_at_ms = 2
    member.withdrawn_by_input_id = member.plan_input_id
    await db.flush()
    assert (
        await ports.prepare(
            db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
        )
        is None
    )
    assert not (await dispatch.progress(db, job_id=job)).actions
    demand, handoff = await ports.facts(db, job_id=job)
    assert demand.state == "UNKNOWN"  # A plan row is not withdrawal proof.
    assert (await ports.advance(db, job_id=job, now_ms=3)).pending


async def test_pending_old_original_work_protects_handoff_and_blocked_input_scope(rack_host):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    db.add(
        AutoBinWork(
            work_line_id=12,
            arrival_id="old",
            device_code="OLD",
            raw_bin_code="B",
            scanned_at=1,
            association="NO_TASK",
        )
    )
    await db.flush()
    demand, handoff = await ports.facts(db, job_id=job)
    assert (demand.state, handoff.state) == ("CONTINUE", "BLOCKED")
    assert (
        await ports.prepare(
            db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
        )
        is None
    )
    db.add(
        AutoTaskInput(
            task_id="task",
            operation="sorting.task.plan_delta@v1",
            operation_id="pending",
            request_timestamp=2,
            first_received_at_ms=2,
            effective_digest="p",
            contract_version="v1",
            effective_request={},
            disposition="BLOCKED",
            requested_revision=3,
        )
    )
    await db.flush()
    assert (await ports.facts(db, job_id=job))[0].state == "UNKNOWN"


async def test_prepare_and_original_references_roll_back_together(rack_host):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    await db.commit()
    member_id = member.id
    action = await ports.prepare(
        db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
    )
    assert action
    await db.rollback()
    original_member = await db.scalar(
        select(AutoInboundSourceMember).where(AutoInboundSourceMember.id == member_id)
    )
    assert original_member.preparation_action_id is None
    assert not (await dispatch.progress(db, job_id=job)).actions


@pytest.mark.parametrize(
    "source_branch,old_obligation",
    [
        (branch, case)
        for branch in ("BIN_CELL", "RACK_SLOT")
        for case in [
            None,
            "PUT",
            "ALARM",
            "UNKNOWN_POSITION",
            "WAIT",
            "NO_WORK",
            "WRONG_OPERATION",
            "HTTP_ERROR",
            "WRONG_CODE",
            "MISSING_FIELDS",
            "WRONG_SUBJECT",
            "REPLAY_PENDING",
            "REPLAY_APPLIED",
            "ROLLBACK",
            "WRONG_FINAL_OPERATION",
            "GENERIC_FINAL_OPERATION",
            "WRONG_FINAL_SOURCE",
            "WRONG_FINAL_BARCODE",
            "INCOMPLETE_TARGET",
            "WRONG_TARGET_TYPE",
        ]
    ]
    + [
        ("BIN_CELL", case)
        for case in ("BIN_TYPE_BOOL", "BIN_TYPE_FLOAT", "BIN_TYPE_EMPTY", "BIN_TYPE_NULL")
    ]
    + [
        ("RACK_SLOT", case)
        for case in (
            "SOURCE_FAILED",
            "SOURCE_CANCELLED",
            "SOURCE_FAILED_WRONG_FACE",
            "SOURCE_CANCELLED_WRONG_POINT",
        )
    ],
)
async def test_final_plate_target_grant_can_rotate_but_never_overrides_old_facts(
    rack_host, old_obligation, source_branch
):
    from app.businesses.smt_auto.models import (
        AutoOutboundDecisionRound,
        AutoOutboundPlate,
    )
    from app.models import WorkLinePosition

    db, ports, dispatch, binding, auth, original, member = rack_host
    task = await db.get(AutoTask, "task")
    task.business_type = "outbound"
    plan = await db.get(AutoTaskInput, member.plan_input_id)
    plan.effective_request = {"target_rack": {"rack_id": "rack", "rack_face": "90"}}
    if source_branch == "RACK_SLOT":
        line = await db.get(WorkLine, 12)
        line.position_bindings = {**line.position_bindings, "SOURCE_TEST": "Q"}
        binding = replace(binding, position_bindings=dict(line.position_bindings))
        original = replace(original, resources=asdict(binding))
        db.add(WorkLinePosition(work_line_id=12, position_code="Q", capacity=None))
    first = RackAuthorization("FIRST_TARGET", plan.id)
    original = replace(
        original, source_kind="AUTO_OUTBOUND_TARGET", source_ref=first.source_ref, initial_face="90"
    )
    job = await ports.register(db, original, authorization=first, original_binding=binding)
    entry = await ports.prepare(
        db, job_id=job, kind="ENTER", authorization=first, face="90", original_binding=binding
    )
    assert entry
    view = await dispatch.progress(db, job_id=job)
    await apply_outcome(db, view.actions[-1], "SUCCEEDED")
    await ports.advance(db, job_id=job, now_ms=2)
    source_member, source_job, source_action = None, None, None
    if source_branch == "BIN_CELL":
        bin_ = AutoBinWork(
            work_line_id=12,
            task_id="task",
            arrival_id="bin",
            device_code="ARM",
            raw_bin_code="B",
            bin_code="B",
            scanned_at=1,
            association="TASK",
        )
        db.add(bin_)
        await db.flush()
        subject_fields = {"bin_work_id": bin_.id}
        ready = {
            "result": "READY",
            "cell_id": "B1",
            "cell_index": 1,
            "bin_type": "TYPE-8",
            "reel_layer": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
        }
        grant_request = {"task_id": "task", "bin_code": "B", "scanned_at": 1}
        source_locator = {"type": "BIN_CELL", "bin_code": "B", "cell_id": "B1"}
        final_request = {
            "task_id": "task",
            "bin_code": "B",
            "cell_id": "B1",
            "barcode": "BARCODE",
            "scanned_at": 3,
        }
    else:
        source_member = AutoOutboundSourceMember(
            task_id="task",
            plan_input_id=plan.id,
            plan_revision=1,
            rack_id="source",
            rack_face="90",
            rack_array_index=0,
        )
        db.add(source_member)
        await db.flush()
        source_auth = RackAuthorization("OUTBOUND_MEMBER", source_member.id)
        source_origin = replace(
            original,
            source_kind="AUTO_OUTBOUND_RETURN",
            source_ref=source_auth.source_ref,
            rack_id="source",
            point="Q",
            cycle_id=origin().cycle_id,
        )
        source_job = await ports.register(
            db, source_origin, authorization=source_auth, original_binding=binding
        )
        source_action = await ports.prepare(
            db,
            job_id=source_job,
            kind="ENTER",
            authorization=source_auth,
            face="90",
            original_binding=binding,
        )
        source_view = await dispatch.progress(db, job_id=source_job)
        source_status = (
            "FAILED"
            if old_obligation in ("SOURCE_FAILED", "SOURCE_FAILED_WRONG_FACE")
            else "CANCELLED"
            if old_obligation in ("SOURCE_CANCELLED", "SOURCE_CANCELLED_WRONG_POINT")
            else "SUCCEEDED"
        )
        await apply_outcome(
            db,
            source_view.actions[-1],
            source_status,
            point="OUTSIDE" if old_obligation == "SOURCE_CANCELLED_WRONG_POINT" else "Q",
            rack_id="source",
            arrival_face="270" if old_obligation == "SOURCE_FAILED_WRONG_FACE" else None,
        )
        await ports.advance(db, job_id=source_job, now_ms=2)
        subject_fields = {"source_preparation_action_id": source_action}
        ready = {
            "result": "READY",
            "rack_id": "source",
            "rack_face": "90",
            "slot_id": "S1",
            "rack_layer": 1,
            "rack_column": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
        }
        grant_request = {"task_id": "task", "rack_id": "source", "rack_face": "90", "arrived_at": 2}
        source_locator = {
            "type": "RACK_SLOT",
            "rack_id": "source",
            "rack_face": "90",
            "slot_id": "S1",
        }
        final_request = {
            "task_id": "task",
            "rack_id": "source",
            "rack_face": "90",
            "slot_id": "S1",
            "barcode": "BARCODE",
            "scanned_at": 3,
        }
    grant = AutoOutboundDecisionRound(
        task_id="task",
        purpose="WORK_PLAN",
        **subject_fields,
        stage_key="grant",
        round_no=1,
        operation="outbound.bin.work_plan@v1"
        if source_branch == "BIN_CELL"
        else "outbound.return_rack.work_plan@v1",
        operation_id="grant",
        message_id=600,
        request_data=grant_request,
        response_snapshot={"http_status": 200, "code": "DECIDED", "data": ready},
        consumed_at_ms=2,
        unresolved=False,
    )
    db.add(grant)
    await db.flush()
    target = {
        "type": "RACK_SLOT",
        "rack_id": "rack",
        "rack_face": "270",
        "slot_id": "S",
        "rack_layer": 1,
        "rack_column": 1,
    }
    plate = AutoOutboundPlate(
        task_id="task",
        bin_work_id=bin_.id if source_branch == "BIN_CELL" else None,
        source_member_id=source_member.id if source_member else None,
        source_rack_job_id=source_job,
        source_preparation_action_id=source_action,
        grant_kind="READY",
        grant_round_id=grant.id,
        grant_snapshot=ready,
        source_locator=source_locator,
        barcode="BARCODE",
        scanned_at_ms=3,
        source_params={},
        source_arrival_snapshot={},
        pick_call_key="pick",
        target_rack_job_id=job,
        target_snapshot=target,
    )
    db.add(plate)
    await db.flush()
    decision = AutoOutboundDecisionRound(
        task_id="task",
        purpose="MATERIAL_DECIDE",
        plate_id=plate.id,
        stage_key="plate",
        round_no=1,
        operation="outbound.bin.material.decide@v1"
        if source_branch == "BIN_CELL"
        else "outbound.rackslot.material.decide@v1",
        operation_id="final",
        message_id=601,
        request_data=final_request,
        response_snapshot={
            "http_status": 200,
            "code": "DECIDED",
            "data": {"result": "ACCEPT", "target_locator": target, "target_preparation": "ROTATE"},
        },
        consumed_at_ms=3,
        unresolved=False,
    )
    db.add(decision)
    await db.flush()
    plate.final_decision_round_id = decision.id
    if old_obligation in ("PUT", "ALARM"):
        plate.put_call_key = "put"
        plate.put_command_code = "original-put"
        plate.put_result_snapshot = {"status": "FAILED"} if old_obligation == "ALARM" else None
    elif old_obligation == "UNKNOWN_POSITION":
        # Public unknown original result leaves no applied current position proof.
        await apply_outcome(db, view.actions[-1], "FAILED", revision=2)
        await ports.advance(db, job_id=job, now_ms=4)
    invalid_bin_types = {
        "BIN_TYPE_BOOL": True,
        "BIN_TYPE_FLOAT": 3.0,
        "BIN_TYPE_EMPTY": "",
        "BIN_TYPE_NULL": None,
    }
    invalid = old_obligation in invalid_bin_types or old_obligation in (
        "WAIT",
        "NO_WORK",
        "WRONG_OPERATION",
        "HTTP_ERROR",
        "WRONG_CODE",
        "MISSING_FIELDS",
        "WRONG_SUBJECT",
        "WRONG_FINAL_OPERATION",
        "GENERIC_FINAL_OPERATION",
        "WRONG_FINAL_SOURCE",
        "WRONG_FINAL_BARCODE",
        "INCOMPLETE_TARGET",
        "WRONG_TARGET_TYPE",
    )
    if old_obligation in invalid_bin_types:
        data = {**ready, "bin_type": invalid_bin_types[old_obligation]}
        grant.response_snapshot = {**grant.response_snapshot, "data": data}
        plate.grant_snapshot = data
    elif old_obligation in ("WAIT", "NO_WORK"):
        data = {"result": old_obligation}
        if old_obligation == "WAIT":
            data["retry_after_ms"] = 1000
        grant.response_snapshot = {**grant.response_snapshot, "data": data}
        plate.grant_snapshot = data
    elif old_obligation == "WRONG_OPERATION":
        grant.operation = (
            "outbound.return_rack.work_plan@v1"
            if source_branch == "BIN_CELL"
            else "outbound.bin.work_plan@v1"
        )
    elif old_obligation == "HTTP_ERROR":
        grant.response_snapshot = {**grant.response_snapshot, "http_status": 503}
    elif old_obligation == "WRONG_CODE":
        grant.response_snapshot = {**grant.response_snapshot, "code": "UNAVAILABLE"}
    elif old_obligation == "MISSING_FIELDS":
        grant.response_snapshot = {**grant.response_snapshot, "data": {"result": "READY"}}
        plate.grant_snapshot = {"result": "READY"}
    elif old_obligation == "WRONG_SUBJECT":
        grant.request_data = {**grant.request_data, "bin_code": "OTHER"}
    if old_obligation == "WRONG_FINAL_OPERATION":
        decision.operation = (
            "outbound.rackslot.material.decide@v1"
            if source_branch == "BIN_CELL"
            else "outbound.bin.material.decide@v1"
        )
    elif old_obligation == "GENERIC_FINAL_OPERATION":
        decision.operation = "outbound.material.decide@v1"
    elif old_obligation == "WRONG_FINAL_SOURCE":
        decision.request_data = {
            **decision.request_data,
            "cell_id" if source_branch == "BIN_CELL" else "slot_id": "OTHER",
        }
    elif old_obligation == "WRONG_FINAL_BARCODE":
        decision.request_data = {**decision.request_data, "barcode": "OTHER"}
    elif old_obligation in ("INCOMPLETE_TARGET", "WRONG_TARGET_TYPE"):
        target = dict(target)
        if old_obligation == "INCOMPLETE_TARGET":
            del target["rack_column"]
        else:
            target["type"] = "BIN_CELL"
        plate.target_snapshot = target
        decision.response_snapshot = {
            **decision.response_snapshot,
            "data": {**decision.response_snapshot["data"], "target_locator": target},
        }
    await db.flush()
    source_invalid = isinstance(old_obligation, str) and old_obligation.startswith("SOURCE_")
    if invalid or source_invalid:
        assert (await ports.facts(db, job_id=job))[1].state != "CLEAR"
    intent_count = await db.scalar(select(func.count()).select_from(ReliableMessage))
    nested = await db.begin_nested() if old_obligation == "ROLLBACK" else None
    rotation_auth = RackAuthorization("PLATE_TARGET", plate.id)
    rotation = await ports.prepare(
        db,
        job_id=job,
        kind="ROTATE",
        authorization=rotation_auth,
        face="270",
        original_binding=binding,
    )
    current = await dispatch.progress(db, job_id=job)
    if old_obligation in (None, "REPLAY_PENDING", "REPLAY_APPLIED", "ROLLBACK"):
        assert rotation and rotation == plate.target_preparation_action_id
        assert current.actions[-1].kind == "ROTATE"
        assert current.actions[-1].source_ref == rotation_auth.source_ref
        assert current.actions[-1].submit_message_id
        if old_obligation in ("REPLAY_PENDING", "REPLAY_APPLIED"):
            if old_obligation == "REPLAY_APPLIED":
                await apply_outcome(db, current.actions[-1], "SUCCEEDED")
                await ports.advance(db, job_id=job, now_ms=5)
            replay_count = await db.scalar(select(func.count()).select_from(ReliableMessage))
            repeated = await ports.prepare(
                db,
                job_id=job,
                kind="ROTATE",
                authorization=rotation_auth,
                face="270",
                original_binding=binding,
            )
            assert repeated == rotation
            assert (
                await db.scalar(select(func.count()).select_from(ReliableMessage)) == replay_count
            )
            after = await dispatch.progress(db, job_id=job)
            assert len(after.actions) == len(current.actions)
            assert after.actions[-1].submit_message_id == current.actions[-1].submit_message_id
        if nested is not None:
            await nested.rollback()
            await db.refresh(plate)
            assert plate.target_preparation_action_id is None
            assert (
                await db.scalar(select(func.count()).select_from(ReliableMessage)) == intent_count
            )
            assert len((await dispatch.progress(db, job_id=job)).actions) == 1
    else:
        assert rotation is None
        assert len(current.actions) == 1
        assert await db.scalar(select(func.count()).select_from(ReliableMessage)) == intent_count


async def test_partial_withdrawal_keeps_demand_and_complete_original_scope_can_close(rack_host):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    task = await db.get(AutoTask, "task")
    task.plan_revision = 2
    plan = AutoTaskInput(
        task_id="task",
        operation="sorting.task.plan_delta@v1",
        operation_id="plan-2",
        request_timestamp=2,
        first_received_at_ms=2,
        effective_digest="d2",
        contract_version="v1",
        effective_request={"added_source_racks": [{"rack_id": "rack"}]},
        disposition="APPLIED",
        applied_plan_revision=2,
    )
    db.add(plan)
    await db.flush()
    second = AutoInboundSourceMember(
        task_id="task",
        plan_input_id=plan.id,
        plan_revision=2,
        rack_id="rack",
        rack_array_index=0,
        rack_job_id=job,
    )
    cancellation = AutoTaskInput(
        task_id="task",
        operation="sorting.task.cancel@v1",
        operation_id="withdraw",
        request_timestamp=3,
        first_received_at_ms=3,
        effective_digest="w",
        contract_version="v1",
        effective_request={
            "cancel_scope": "PLAN_MEMBERS",
            "source_racks": [
                {"plan_revision": 1, "rack_id": "rack"},
                {"plan_revision": 2, "rack_id": "rack"},
            ],
        },
        disposition="APPLIED",
    )
    db.add_all([second, cancellation])
    await db.flush()
    member.withdrawn_by_input_id, member.withdrawn_at_ms = cancellation.id, 3
    await db.flush()
    assert (await ports.facts(db, job_id=job))[0].state == "CONTINUE"
    second.withdrawn_by_input_id, second.withdrawn_at_ms = cancellation.id, 3
    await db.flush()
    demand, handoff = await ports.facts(db, job_id=job)
    assert (demand.state, handoff.state) == ("NONE", "CLEAR")
    assert (
        await ports.prepare(
            db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
        )
        is None
    )


@pytest.mark.parametrize("gate", ["disabled", "line_version", "predecessor"])
async def test_return_arrival_keeps_first_original_confirmation(rack_host, monkeypatch, gate):
    db, ports, dispatch, binding, _, original, inbound = rack_host
    (await db.get(AutoTask, "task")).business_type = "outbound"
    plan = await db.get(AutoTaskInput, inbound.plan_input_id)
    plan.effective_request = {"added_direct_pick_sources": [{"rack_id": "rack", "rack_face": "90"}]}
    member = AutoOutboundSourceMember(
        task_id="task",
        plan_input_id=plan.id,
        plan_revision=1,
        rack_id="rack",
        rack_face="90",
        rack_array_index=0,
    )
    db.add(member)
    await db.flush()
    auth = RackAuthorization("OUTBOUND_MEMBER", member.id)
    original = replace(
        original, source_kind="AUTO_OUTBOUND_RETURN", source_ref=auth.source_ref, initial_face="90"
    )
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    entry = await ports.prepare(
        db, job_id=job, kind="ENTER", authorization=auth, face="90", original_binding=binding
    )
    assert entry == member.preparation_action_id
    view = await dispatch.progress(db, job_id=job)
    await apply_outcome(db, view.actions[-1], "SUCCEEDED")

    line = await db.get(WorkLine, 12)
    transport = dispatch.capabilities.transport
    read_predecessors = transport.list_unclosed_predecessors

    async def incomplete(session, **scope):
        return replace(await read_predecessors(session, **scope), complete=False)

    if gate == "disabled":
        line.is_enabled = False
    elif gate == "line_version":
        line.version += 1
    else:
        monkeypatch.setattr(transport, "list_unclosed_predecessors", incomplete)
    view = await ports.advance(db, job_id=job, now_ms=10)
    assert view.position_action_id == entry
    assert view.actions[-1].phase == "APPLIED"
    assert view.review_reason == ("UNRESOLVED_PREDECESSOR" if gate == "predecessor" else None)
    assert member.arrived_at_ms == 10

    line.is_enabled, line.version = True, binding.work_line_version
    monkeypatch.setattr(transport, "list_unclosed_predecessors", read_predecessors)
    recovered = await ports.advance(db, job_id=job, now_ms=20)
    assert recovered.review_reason is None
    assert recovered.position_action_id == entry
    assert member.arrived_at_ms == 10


@pytest.mark.parametrize("gate", ["disabled", "line_version"])
async def test_departure_waits_for_current_admission_and_consumes_original_decision(
    rack_host, gate
):
    db, ports, dispatch, binding, auth, original, member = rack_host
    job = await ports.register(db, original, authorization=auth, original_binding=binding)
    await ports.prepare(
        db, job_id=job, kind="ENTER", authorization=auth, face=None, original_binding=binding
    )
    view = await dispatch.progress(db, job_id=job)
    await apply_outcome(db, view.actions[-1], "SUCCEEDED")
    cancellation = AutoTaskInput(
        task_id="task",
        operation="sorting.task.cancel@v1",
        operation_id="withdraw",
        request_timestamp=2,
        first_received_at_ms=2,
        effective_digest="w",
        contract_version="v1",
        effective_request={
            "cancel_scope": "PLAN_MEMBERS",
            "source_racks": [{"plan_revision": 1, "rack_id": "rack"}],
        },
        disposition="APPLIED",
    )
    db.add(cancellation)
    await db.flush()
    member.withdrawn_by_input_id, member.withdrawn_at_ms = cancellation.id, 2

    async def source_clear(session, *, job_id):
        return True

    ports = AutoRackPorts(dispatch, inbound=SimpleNamespace(source_clear=source_clear))
    root = (await ports.advance(db, job_id=job, now_ms=3)).actions[-1]
    assert root.kind == "DEPARTURE_DECIDE"
    await respond(
        db,
        root.decision_operation_id,
        200,
        "DECIDED",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "Z"}},
    )
    intent_count = await db.scalar(select(func.count()).select_from(ReliableMessage))
    line = await db.get(WorkLine, 12)
    if gate == "disabled":
        line.is_enabled = False
    else:
        line.version += 1
    blocked = await ports.advance(db, job_id=job, now_ms=4)
    assert [(a.kind, a.phase) for a in blocked.actions] == [
        ("ENTER", "APPLIED"),
        ("DEPARTURE_DECIDE", "READY"),
    ]
    assert blocked.actions[-1].id == root.id
    assert blocked.actions[-1].decision_consumed_at_ms == 4
    assert await db.scalar(select(func.count()).select_from(ReliableMessage)) == intent_count

    line.is_enabled, line.version = True, binding.work_line_version
    resumed = await ports.advance(db, job_id=job, now_ms=5)
    depart = resumed.actions[-1]
    assert depart.kind == "DEPART"
    assert depart.previous_action_id == root.id
    assert await db.scalar(select(func.count()).select_from(ReliableMessage)) == intent_count + 1

    line.is_enabled = False
    await respond(
        db,
        depart.submit_operation_id,
        202,
        "RECEIVED",
        {"transport_task_id": depart.transport_task_id},
    )
    completed = await ports.advance(db, job_id=job, now_ms=6)
    assert completed.state == "COMPLETED"
    assert completed.actions[-1].id == depart.id
