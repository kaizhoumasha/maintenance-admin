"""AUTO host evidence through the real shared WMS callback receiver and public outbox ports."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import (
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.models import IntegrationExchange, ReliableMessage, WorkLine


async def start(flow, *, direction="inbound"):
    issued = flow.message(
        "issued",
        dict(
            task_id="task",
            task_type="AUTO",
            business_type=direction,
            workline_code="AUTO-TEST",
            queue_revision=1,
            dispatch_sequence=1,
        ),
    )
    assert (await flow.deliver(issued)).status_code == 202
    assert await flow.transactions.advance("PREPARE_NEXT", flow.line_id, now_ms=100) == "task"
    return flow.message(
        "plan_delta",
        dict(
            task_id="task",
            plan_revision=1,
            added_source_racks=[{"rack_id": "SOURCE-1"}, {"rack_id": "SOURCE-2"}],
            added_bin_source_racks=[{"rack_id": "BIN-1", "rack_face": ["90", "270"]}],
        ),
    )


async def snapshot(flow, body):
    async with flow.factory() as session:
        task = await session.get(AutoTask, "task")
        original = await session.scalar(
            select(AutoTaskInput).where(AutoTaskInput.operation_id == body["operation_id"])
        )
        receipt = await session.scalar(
            select(IntegrationExchange)
            .where(
                IntegrationExchange.role == "RECEIPT", IntegrationExchange.receipt_active.is_(True)
            )
            .where(IntegrationExchange.operation_id == body["operation_id"])
        )
        from tests.helpers import get_receipt_by_id

        receipt = await get_receipt_by_id(session, receipt.receipt_id) if receipt else None
        members = list(
            await session.scalars(
                select(AutoInboundSourceMember).order_by(AutoInboundSourceMember.rack_array_index)
            )
        )
        prepares = list(await session.scalars(select(AutoTaskRequest)))
        return task, original, receipt, members, prepares


async def test_original_early_plan_is_durable_and_only_original_retry_applies(auto_receiver):
    f = auto_receiver
    plan = await start(f)
    first = await f.deliver(plan)
    assert first.status_code == 503
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert task.status == "PREPARING" and task.plan_revision == 0 and members == []
    assert original.disposition == "BLOCKED" and original.block_reason == "PREPARE_PENDING"
    assert original.request_timestamp == original.first_received_at_ms == plan["timestamp"]
    assert receipt.status_code == 503 and receipt.body == first.body
    identity = (prepares[0].message_id, prepares[0].operation_id)
    assert f.notices == ["smt_auto"]
    await f.accept_prepare()
    assert await f.transactions.advance("PREPARE_RESULT", "task", now_ms=200) == "PREPARING"
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert task.prepare_accepted_at_ms == 200 and task.plan_revision == 0 and members == []
    assert original.disposition == "BLOCKED" and receipt.status_code == 503
    retry = await f.deliver(plan)
    assert retry.status_code == 202 and retry.body["data"] == {}
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert task.status == "EXECUTING" and task.plan_revision == original.applied_plan_revision == 1
    assert original.disposition == "APPLIED" and receipt.status_code == 202
    assert [(m.rack_id, m.rack_array_index, m.plan_input_id) for m in members] == [
        ("SOURCE-1", 0, original.id),
        ("SOURCE-2", 1, original.id),
    ]
    assert len(prepares) == 1 and (prepares[0].message_id, prepares[0].operation_id) == identity
    duplicate = await f.deliver(plan)
    assert duplicate.status_code == 200 and duplicate.body["data"] == {}
    assert f.notices == ["smt_auto", "smt_auto"]
    async with f.factory() as session:
        supply = list(
            await session.scalars(
                select(AutoSupplyMember).order_by(AutoSupplyMember.face_array_index)
            )
        )
        assert [(m.rack_id, m.rack_face, m.plan_input_id) for m in supply] == [
            ("BIN-1", "90", original.id),
            ("BIN-1", "270", original.id),
        ]


@pytest.mark.parametrize("change", ["timestamp", "content"])
async def test_blocked_original_identity_conflicts_stay_stable(auto_receiver, change):
    f = auto_receiver
    plan = await start(f)
    pending = await f.deliver(plan)
    changed = deepcopy(plan)
    if change == "timestamp":
        changed["timestamp"] += 1
    else:
        changed["data"]["added_source_racks"].reverse()
    for _ in range(2):
        conflict = await f.deliver(changed)
        assert conflict.status_code == 409 and conflict.body["data"] == {
            "reason_code": "IDEMPOTENCY_CONFLICT"
        }
    _, original, receipt, members, _ = await snapshot(f, plan)
    assert (
        original.effective_request == plan["data"]
        and original.request_timestamp == plan["timestamp"]
    )
    assert original.disposition == "BLOCKED" and members == [] and receipt.body == pending.body


async def test_recheck_outer_rollback_keeps_all_original_facts_and_no_notification(auto_receiver):
    f = auto_receiver
    plan = await start(f)
    pending = await f.deliver(plan)
    await f.accept_prepare()
    await f.transactions.advance("PREPARE_RESULT", "task", now_ms=200)
    notices = list(f.notices)
    assert (await f.deliver(plan, rollback=True)).status_code == 202
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert task.plan_revision == 0 and task.status == "PREPARING" and members == []
    assert original.disposition == "BLOCKED" and original.applied_plan_revision is None
    assert receipt.body == pending.body and f.notices == notices and len(prepares) == 1
    async with f.factory() as session:
        assert list(await session.scalars(select(AutoSupplyMember))) == []
    assert (await f.deliver(plan)).status_code == 202


async def test_rolled_back_issued_and_prepare_leave_no_new_intent(auto_receiver):
    f = auto_receiver
    issued = f.message(
        "issued",
        dict(
            task_id="task",
            task_type="AUTO",
            business_type="inbound",
            workline_code="AUTO-TEST",
            queue_revision=1,
            dispatch_sequence=1,
        ),
    )
    assert (await f.deliver(issued, rollback=True)).status_code == 202
    async with f.factory() as session:
        for model in (AutoTask, AutoTaskInput, IntegrationExchange, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert f.notices == []
    assert (await f.deliver(issued)).status_code == 202
    async with f.factory() as session:
        await f.transactions.host.prepare_next(session, work_line_id=f.line_id, now_ms=100)
        await session.rollback()
    async with f.factory() as session:
        assert (await session.get(AutoTask, "task")).status == "QUEUED"
        for model in (AutoTaskRequest, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("recovery", ["disabled", "cancelled"])
async def test_original_prepare_recovery_survives_disabled_or_cancelled_line(
    auto_receiver, recovery
):
    f = auto_receiver
    plan = await start(f)
    assert (await f.deliver(plan)).status_code == 503
    before = (await snapshot(f, plan))[4][0]
    if recovery == "cancelled":
        cancel = f.message("cancel", dict(task_id="task", cancel_scope="TASK"))
        assert (await f.deliver(cancel)).status_code == 202
    async with f.factory.begin() as session:
        line = await session.get(WorkLine, f.line_id)
        line.is_enabled, line.business_code = False, "changed-owner"
    await f.accept_prepare()
    status = await f.transactions.advance("PREPARE_RESULT", "task", now_ms=200)
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert status == ("CANCELLED" if recovery == "cancelled" else "PREPARING")
    assert task.workline_code == "AUTO-TEST" and task.work_line_id == f.line_id
    assert (
        prepares[0].message_id == before.message_id
        and prepares[0].operation_id == before.operation_id
    )
    assert not prepares[0].unresolved and prepares[0].consumed_at_ms == 200
    assert original.disposition == "BLOCKED" and receipt.status_code == 503 and members == []
    assert await f.transactions.advance("PREPARE_NEXT", f.line_id, now_ms=201) is None
    if recovery == "cancelled":
        assert not task.pending and (await f.deliver(plan)).status_code == 409
    else:
        async with f.factory.begin() as session:
            line = await session.get(WorkLine, f.line_id)
            line.business_code = "smt_auto"
        assert (await f.deliver(plan)).status_code == 409
        async with f.factory.begin() as session:
            (await session.get(WorkLine, f.line_id)).is_enabled = True
        assert (await f.deliver(plan)).status_code == 202


async def test_outbound_members_share_original_atomic_receiver_application(auto_receiver):
    f = auto_receiver
    await start(f, direction="outbound")
    plan = f.message(
        "plan_delta",
        dict(
            task_id="task",
            plan_revision=1,
            target_rack={"rack_id": "TARGET", "rack_face": "90"},
            added_bin_source_racks=[{"rack_id": "BIN", "rack_face": ["90", "270"]}],
            added_return_racks=[{"rack_id": "RETURN", "rack_faces": ["90", "270"]}],
        ),
    )
    assert (await f.deliver(plan)).status_code == 503
    await f.accept_prepare()
    await f.transactions.advance("PREPARE_RESULT", "task", now_ms=200)
    assert (await f.deliver(plan, rollback=True)).status_code == 202
    async with f.factory() as session:
        for model in (AutoSupplyMember, AutoOutboundSourceMember):
            assert list(await session.scalars(select(model))) == []
    assert (await f.deliver(plan)).status_code == 202
    task, original, receipt, _, _ = await snapshot(f, plan)
    assert task.plan_revision == original.applied_plan_revision == 1 and receipt.status_code == 202
    async with f.factory() as session:
        for model, rack in ((AutoSupplyMember, "BIN"), (AutoOutboundSourceMember, "RETURN")):
            members = list(await session.scalars(select(model).order_by(model.id)))
            assert [(m.rack_id, m.rack_face, m.plan_input_id) for m in members] == [
                (rack, "90", original.id),
                (rack, "270", original.id),
            ]
    assert (await f.deliver(plan)).status_code == 200


async def test_serialization_exhaustion_retains_committed_original_prepare(auto_receiver):
    from types import SimpleNamespace

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    f = auto_receiver
    plan = await start(f)
    assert (await f.deliver(plan)).status_code == 503
    before = (await snapshot(f, plan))[4][0]
    sessions = []

    class FailingResults:
        async def get_result(self, session, **identity):
            sessions.append(session)
            assert identity == dict(
                message_id=before.message_id,
                operation=before.operation,
                operation_id=before.operation_id,
            )
            if session.get_bind().dialect.name == "postgresql":
                assert await session.scalar(text("SHOW transaction_isolation")) == "serializable"
            task = await session.get(AutoTask, "task")
            task.pending = False
            await session.flush()
            raise DBAPIError(None, None, SimpleNamespace(sqlstate="40001"))

    original_wms = f.transactions.host.wms
    f.transactions.host.wms = FailingResults()
    with pytest.raises(DBAPIError):
        await f.transactions.advance("PREPARE_RESULT", "task", now_ms=200)
    assert len({id(s) for s in sessions}) == 3
    task, original, receipt, members, prepares = await snapshot(f, plan)
    assert task.pending and task.prepare_accepted_at_ms is None and members == []
    assert original.disposition == "BLOCKED" and receipt.status_code == 503
    assert prepares[0].unresolved and prepares[0].message_id == before.message_id
    assert prepares[0].operation_id == before.operation_id
    f.transactions.host.wms = original_wms
    await f.accept_prepare()
    await f.transactions.advance("PREPARE_RESULT", "task", now_ms=201)
    assert (await f.deliver(plan)).status_code == 202


@pytest.mark.parametrize("configured", [None, 100])
async def test_first_plan_deadline_freezes_original_prepare_choice_across_restart(
    auto_receiver, configured
):
    from app.businesses.smt_auto.task_service import AutoTaskService

    f = auto_receiver
    issued = f.message(
        "issued",
        dict(
            task_id="task",
            task_type="AUTO",
            business_type="inbound",
            workline_code="AUTO-TEST",
            queue_revision=1,
            dispatch_sequence=1,
        ),
    )
    assert (await f.deliver(issued)).status_code == 202
    f.transactions.host.first_plan_wait_ms = configured
    assert await f.transactions.advance("PREPARE_NEXT", f.line_id, now_ms=1) == "task"
    await f.accept_prepare()
    assert await f.transactions.advance("PREPARE_RESULT", "task", now_ms=0) == "PREPARING"
    async with f.factory.begin() as session:
        task = await session.get(AutoTask, "task")
        assert task.prepare_accepted_at_ms == 0
        assert task.first_plan_wait_ms == configured
        assert task.first_plan_deadline_ms == (None if configured is None else 100)
        # New process/config cannot retroactively install or extend this original choice.
        host = AutoTaskService(wms=f.transactions.host.wms, first_plan_wait_ms=9999)
        await host.advance_prepare(session, task_id="task", now_ms=5000)
        assert task.prepare_accepted_at_ms == 0
        assert task.first_plan_wait_ms == configured
        assert task.first_plan_deadline_ms == (None if configured is None else 100)
