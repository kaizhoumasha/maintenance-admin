"""Original task ownership and the inbound retention boundary through the shared receiver."""

from sqlalchemy import func, select

from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.smt_auto.archive import RETENTION_MS, Archive
from app.businesses.smt_auto.models import AutoTask, AutoTaskInput
from app.businesses.smt_auto.rack_ports import AutoRackPorts


async def test_cancelled_inbound_archive_keeps_original_owner_and_stable_refusal(auto_receiver):
    host = auto_receiver
    issued = host.message(
        "issued",
        dict(
            task_id="archive",
            task_type="AUTO",
            business_type="inbound",
            workline_code="AUTO-TEST",
            queue_revision=1,
            dispatch_sequence=1,
        ),
    )
    assert (await host.deliver(issued)).status_code == 202
    cancel = host.message("cancel", dict(task_id="archive", cancel_scope="TASK"))
    cancel["timestamp"] = 1
    assert (await host.deliver(cancel)).status_code == 202
    archive = Archive(None, AutoRackPorts(RackDispatch("smt_auto", host.transactions.capabilities)))
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "archive")
        closed = task.closed_at_ms
        assert closed > cancel["timestamp"]
        assert not await archive.clean(
            session, task_id=task.task_id, now_ms=cancel["timestamp"] + RETENTION_MS
        )
        assert not await archive.clean(
            session, task_id=task.task_id, now_ms=closed + RETENTION_MS - 1
        )
    assert (await host.deliver(issued)).status_code == 200
    async with host.factory.begin() as session:
        assert await archive.clean(session, task_id="archive", now_ms=closed + RETENTION_MS)
        assert await session.scalar(select(func.count()).select_from(AutoTaskInput)) == 0
    first = await host.deliver(issued)
    second = await host.deliver(issued)
    assert first.status_code == 409 and first.body == second.body
    assert first.body["timestamp"] == closed + RETENTION_MS
    assert first.body["data"] == {"reason_code": "STATE_CONFLICT"}
    changed = {**issued, "data": {**issued["data"], "dispatch_sequence": 2}}
    assert (await host.deliver(changed)).body["data"] == {"reason_code": "IDEMPOTENCY_CONFLICT"}


async def test_completed_archive_removes_closed_plate_dependency_chain(auto_receiver):
    from types import SimpleNamespace

    from app.businesses.smt_auto.models import (
        AutoBinWork,
        AutoInboundDecisionRound,
        AutoInboundPlate,
    )

    host = auto_receiver
    rack_ports = AutoRackPorts(RackDispatch("smt_auto", host.transactions.capabilities))

    async def proven_completion(session, task, *, now_ms):
        # This case isolates database deletion from the separately tested result readers.
        return {"original_completion": task.task_id}

    archive = Archive(SimpleNamespace(retention_basis=proven_completion), rack_ports)
    async with host.factory.begin() as session:
        session.add(
            AutoTask(
                task_id="plate-task",
                business_type="inbound",
                work_line_id=host.line_id,
                workline_code="AUTO-TEST",
                issued_operation_id="issued",
                issued_timestamp=1,
                issued_data={},
                dispatch_sequence=1,
                status="COMPLETED",
                pending=False,
                closed_at_ms=100,
                direct_obligations_closed_at_ms=100,
            )
        )
        await session.flush()
        work = AutoBinWork(
            task_id="plate-task",
            business_type="inbound",
            work_line_id=host.line_id,
            arrival_id="arrival",
            device_code="scan",
            raw_bin_code="bin",
            bin_code="bin",
            scanned_at=1,
            association="TASK",
            pending=False,
        )
        session.add(work)
        await session.flush()
        initial = AutoInboundDecisionRound(
            task_id="plate-task",
            purpose="WORK_PLAN",
            bin_work_id=work.id,
            stage_key="first",
            round_no=1,
            operation="putaway.work_plan@v1",
            operation_id="plan",
            message_id=1,
            request_data={},
            unresolved=False,
        )
        session.add(initial)
        await session.flush()
        plate = AutoInboundPlate(
            task_id="plate-task",
            bin_work_id=work.id,
            grant_kind="READY",
            grant_round_id=initial.id,
            grant_snapshot={},
            source_locator={},
            source_params={},
            source_arrival_snapshot={},
            pick_call_key="pick",
            pending=False,
        )
        session.add(plate)
        await session.flush()
        decision = AutoInboundDecisionRound(
            task_id="plate-task",
            purpose="MATERIAL_DECIDE",
            plate_id=plate.id,
            stage_key="decision",
            round_no=1,
            operation="putaway.material.decide@v1",
            operation_id="decision",
            message_id=2,
            request_data={},
            unresolved=False,
        )
        session.add(decision)
        await session.flush()
        plate.final_decision_round_id = decision.id
        session.add(
            AutoInboundPlate(
                task_id="plate-task",
                bin_work_id=work.id,
                grant_kind="NEXT",
                grant_parent_plate_id=plate.id,
                grant_snapshot={},
                source_locator={},
                source_params={},
                source_arrival_snapshot={},
                pick_call_key="next",
                pending=False,
            )
        )
    async with host.factory.begin() as session:
        assert await archive.clean(session, task_id="plate-task", now_ms=100 + RETENTION_MS)
        for model in (AutoInboundPlate, AutoInboundDecisionRound, AutoBinWork):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (await session.get(AutoTask, "plate-task")).details_cleaned


async def test_cancelled_archive_waits_for_late_original_prepare_recovery(auto_receiver):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = auto_receiver
    issued = host.message(
        "issued",
        dict(
            task_id="late-prepare",
            task_type="AUTO",
            business_type="inbound",
            workline_code="AUTO-TEST",
            queue_revision=1,
            dispatch_sequence=1,
        ),
    )
    assert (await host.deliver(issued)).status_code == 202
    assert (
        await host.transactions.advance("PREPARE_NEXT", host.line_id, now_ms=100) == "late-prepare"
    )
    cancel = host.message("cancel", dict(task_id="late-prepare", cancel_scope="TASK"))
    assert (await host.deliver(cancel)).status_code == 202
    dispatch = RackDispatch("smt_auto", host.transactions.capabilities)
    archive = Archive(
        TaskFlow(host.transactions.host.wms, dispatch=dispatch), AutoRackPorts(dispatch)
    )
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "late-prepare")
        recovered_at = task.closed_at_ms + 10_000
        assert not await archive.clean(
            session, task_id=task.task_id, now_ms=recovered_at + RETENTION_MS
        )
    await host.accept_prepare()
    assert (
        await host.transactions.advance("PREPARE_RESULT", "late-prepare", now_ms=recovered_at)
        == "CANCELLED"
    )
    async with host.factory.begin() as session:
        assert not await archive.clean(
            session, task_id="late-prepare", now_ms=recovered_at + RETENTION_MS - 1
        )
        assert await archive.clean(
            session, task_id="late-prepare", now_ms=recovered_at + RETENTION_MS
        )
