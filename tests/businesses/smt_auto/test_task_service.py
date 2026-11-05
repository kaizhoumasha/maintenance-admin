from dataclasses import replace

import pytest
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    Issued,
    PlanDelta,
)
from app.businesses.smt_auto.models import (
    TABLES,
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.businesses.smt_auto.task_service import AutoTaskService
from app.integrations.wms_callback import CallbackContext, Conflict, Rejected
from app.models.base import Base
from app.models.workline import WorkLine


@pytest.fixture
async def session(request, postgres_tables):
    metadata = MetaData(schema=Base.metadata.schema)
    for table in (*TABLES.values(), WorkLine.__table__):
        table.to_metadata(metadata)
    import os
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        if getattr(request, "param", "sqlite") == "postgres":
            url = os.environ.get("TEST_DATABASE_URL")
            if not url:
                pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL host tests")
            engine = await stack.enter_async_context(postgres_tables(url, "auto_host", metadata))
        else:
            engine = create_async_engine(
                "sqlite+aiosqlite://",
                execution_options={"schema_translate_map": {Base.metadata.schema: None}},
            )
            stack.push_async_callback(engine.dispose)
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        async with engine.begin() as conn:
            await conn.execute(
                WorkLine.__table__.insert().values(
                    id=1,
                    line_code="L1",
                    line_name="AUTO test line",
                    business_code="smt_auto",
                    is_enabled=True,
                )
            )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as value:
            yield value


def ctx(session, operation, operation_id, payload):
    return CallbackContext(
        session, 1, operation_id, 100, payload.model_dump(mode="json"), operation=operation
    )


async def prepared(session, task_id):
    task = await session.get(AutoTask, task_id)
    task.status, task.prepare_accepted_at_ms = "PREPARING", 101
    session.add(
        AutoTaskRequest(
            task_id=task_id,
            purpose="PREPARE",
            operation="sorting.task.prepare@v1",
            operation_id=f"prepare-{task_id}",
            message_id=sum(map(ord, task_id)),
            request_data={"task_id": task_id, "workline_code": task.workline_code},
            unresolved=False,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_plan_versions_must_be_contiguous_and_duplicate_is_idempotent(session):
    service = AutoTaskService()
    issued = Issued(
        task_id="t1",
        task_type="AUTO",
        business_type="inbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await service.accept_task_issued(ctx(session, "sorting.task.issued@v1", "i1", issued), issued)
    await prepared(session, issued.task_id)
    delta = PlanDelta(
        task_id="t1",
        plan_revision=1,
        added_source_racks=[{"rack_id": "R1"}],
    )
    result = await service.accept_task_plan_delta(
        ctx(session, "sorting.task.plan_delta@v1", "p1", delta), delta
    )
    assert result.data == {}
    assert (await session.get(AutoTask, "t1")).plan_revision == 1
    task = await session.get(AutoTask, "t1")
    assert task.work_line_id == 1
    source = await session.scalar(
        select(AutoInboundSourceMember).where(AutoInboundSourceMember.task_id == "t1")
    )
    assert source.rack_id == "R1" and source.plan_input_id is not None
    duplicate = await service.accept_task_plan_delta(
        ctx(session, "sorting.task.plan_delta@v1", "p1", delta), delta
    )
    assert duplicate.__class__.__name__ == "Duplicate"
    missing = delta.model_copy(update={"plan_revision": 3})
    assert isinstance(
        await service.accept_task_plan_delta(
            ctx(session, "sorting.task.plan_delta@v1", "p3", missing), missing
        ),
        Conflict,
    )


@pytest.mark.asyncio
async def test_cancel_task_is_transactional_and_idempotent(session):
    service = AutoTaskService()
    issued = Issued(
        task_id="t2",
        task_type="AUTO",
        business_type="outbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await service.accept_task_issued(ctx(session, "sorting.task.issued@v1", "i2", issued), issued)
    cancel = Cancel(
        task_id="t2",
        cancel_scope="TASK",
    )
    await service.accept_task_cancel(ctx(session, "sorting.task.cancel@v1", "c2", cancel), cancel)
    task = await session.get(AutoTask, "t2")
    assert task.status == "CANCELLED" and task.pending is False


@pytest.mark.asyncio
async def test_plan_member_cancel_withdraws_exact_member_and_rejects_other_scopes(session):
    service = AutoTaskService()
    issued = Issued(
        task_id="t3",
        task_type="AUTO",
        business_type="inbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await service.accept_task_issued(ctx(session, "sorting.task.issued@v1", "i3", issued), issued)
    await prepared(session, issued.task_id)
    delta = PlanDelta(
        task_id="t3",
        plan_revision=1,
        added_bin_source_racks=[{"rack_id": "R1", "rack_face": ["A"]}],
    )
    await service.accept_task_plan_delta(
        ctx(session, "sorting.task.plan_delta@v1", "p3", delta), delta
    )
    plan_input = await session.scalar(
        select(AutoTaskInput).where(AutoTaskInput.operation_id == "p3")
    )
    member = await session.scalar(select(AutoSupplyMember).where(AutoSupplyMember.task_id == "t3"))
    assert member.plan_input_id == plan_input.id
    cancel = Cancel(
        task_id="t3",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"rack_id": "R1", "rack_face": ["A"], "plan_revision": 1}],
    )
    result = await service.accept_task_cancel(
        ctx(session, "sorting.task.cancel@v1", "c3", cancel), cancel
    )
    member = await session.get(AutoSupplyMember, member.id)
    assert result.data["withdrawn_member_ids"] == [member.id]
    assert member.withdrawn_by_input_id is not None

    unsupported = Cancel(
        task_id="t3",
        cancel_scope="PLAN_MEMBERS",
        source_racks=[{"rack_id": "R1", "plan_revision": 1}],
    )
    with pytest.raises(Conflict):
        await service.accept_task_cancel(
            ctx(session, "sorting.task.cancel@v1", "c4", unsupported), unsupported
        )


@pytest.mark.asyncio
async def test_source_and_direct_member_cancel_follow_task_direction(session):
    service = AutoTaskService()
    issued = Issued(
        task_id="t4",
        task_type="AUTO",
        business_type="outbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await service.accept_task_issued(ctx(session, "sorting.task.issued@v1", "i4", issued), issued)
    await prepared(session, issued.task_id)
    delta = PlanDelta(
        task_id="t4",
        plan_revision=1,
        target_rack={"rack_id": "TARGET", "rack_face": "A"},
        added_return_racks=[{"rack_id": "R1", "rack_faces": ["A"]}],
    )
    await service.accept_task_plan_delta(
        ctx(session, "sorting.task.plan_delta@v1", "p4", delta), delta
    )
    plan_input = await session.scalar(
        select(AutoTaskInput).where(AutoTaskInput.operation_id == "p4")
    )
    member = await session.scalar(
        select(AutoOutboundSourceMember).where(AutoOutboundSourceMember.task_id == "t4")
    )
    assert member.plan_input_id == plan_input.id
    direct = Cancel(
        task_id="t4",
        cancel_scope="PLAN_MEMBERS",
        direct_pick_sources=[{"rack_id": "R1", "rack_face": "A", "plan_revision": 1}],
    )
    result = await service.accept_task_cancel(
        ctx(session, "sorting.task.cancel@v1", "c4", direct), direct
    )
    member = await session.get(AutoOutboundSourceMember, member.id)
    assert result.data["withdrawn_member_ids"] == [member.id]
    assert member.withdrawn_by_input_id is not None


@pytest.mark.asyncio
async def test_plan_application_rolls_back_task_and_input_together(session):
    service = AutoTaskService()
    issued = Issued(
        task_id="t5",
        task_type="AUTO",
        business_type="inbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await service.accept_task_issued(ctx(session, "sorting.task.issued@v1", "i5", issued), issued)
    await prepared(session, issued.task_id)
    await session.commit()
    delta = PlanDelta(task_id="t5", plan_revision=1, added_source_racks=[{"rack_id": "R1"}])
    with pytest.raises(RuntimeError):
        async with session.begin():
            await service.accept_task_plan_delta(
                ctx(session, "sorting.task.plan_delta@v1", "p5", delta), delta
            )
            raise RuntimeError("abort")
    task = await session.get(AutoTask, "t5")
    assert task.plan_revision == 0
    assert (
        await session.scalar(select(AutoTaskInput).where(AutoTaskInput.operation_id == "p5"))
        is None
    )


async def issue(session, *, direction="inbound"):
    payload = Issued(
        task_id="task",
        task_type="AUTO",
        business_type=direction,
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    await AutoTaskService().accept_task_issued(ctx(session, "issued", "issue", payload), payload)
    await session.flush()
    return await session.get(AutoTask, "task")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,revision", [("EXECUTING", 0), ("PREPARING", 1), ("COMPLETED", 1)])
async def test_task_cancellation_rejects_started_or_finished_tasks(session, status, revision):
    task = await issue(session)
    task.status, task.plan_revision = status, revision
    payload = Cancel(task_id="task", cancel_scope="TASK")
    with pytest.raises(Conflict):
        await AutoTaskService().accept_task_cancel(
            ctx(session, "cancel", "cancel", payload), payload
        )
    assert task.status == status and task.pending


@pytest.mark.asyncio
async def test_preparing_cancellation_preserves_original_recovery_pending(session):
    task = await issue(session)
    task.status = "PREPARING"
    payload = Cancel(task_id="task", cancel_scope="TASK")
    await AutoTaskService().accept_task_cancel(ctx(session, "cancel", "cancel", payload), payload)
    assert task.status == "CANCELLED" and task.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_member_cancellation_supports_mixed_selectors_without_partial_missing_matches(
    session, direction
):
    await issue(session, direction=direction)
    await prepared(session, "task")
    resources = {"added_bin_source_racks": [{"rack_id": "BIN", "rack_face": ["A", "B"]}]}
    if direction == "inbound":
        resources["added_source_racks"] = [{"rack_id": "SOURCE"}]
        selectors = {"source_racks": [{"rack_id": "SOURCE", "plan_revision": 1}]}
        model = AutoInboundSourceMember
    else:
        resources.update(
            target_rack={"rack_id": "TARGET", "rack_face": "A"},
            added_return_racks=[{"rack_id": "SOURCE", "rack_faces": ["A"]}],
        )
        selectors = {
            "direct_pick_sources": [{"rack_id": "SOURCE", "rack_face": "A", "plan_revision": 1}]
        }
        model = AutoOutboundSourceMember
    service = AutoTaskService()
    delta = PlanDelta(task_id="task", plan_revision=1, **resources)
    await service.accept_task_plan_delta(ctx(session, "plan", "plan", delta), delta)
    await session.flush()
    invalid = Cancel(
        task_id="task",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"rack_id": "BIN", "rack_face": ["A", "MISSING"], "plan_revision": 1}],
    )
    with pytest.raises(Conflict):
        await service.accept_task_cancel(ctx(session, "cancel", "invalid", invalid), invalid)
    assert all(m.withdrawn_at_ms is None for m in await session.scalars(select(AutoSupplyMember)))
    payload = Cancel(
        task_id="task",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"rack_id": "BIN", "rack_face": ["A"], "plan_revision": 1}],
        **selectors,
    )
    result = await service.accept_task_cancel(ctx(session, "cancel", "mixed", payload), payload)
    assert len(result.data["withdrawn_member_ids"]) == 2
    assert (await session.scalar(select(model))).withdrawn_at_ms == 100
    faces = {
        m.rack_face: m.withdrawn_at_ms for m in await session.scalars(select(AutoSupplyMember))
    }
    assert faces == {"A": 100, "B": None}
    # A new legitimate request for already withdrawn members remains a successful no-op.
    await service.accept_task_cancel(ctx(session, "cancel", "again", payload), payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"is_enabled": False}, {"business_code": "smt_manual"}, {"line_code": "RENAMED"}]
)
async def test_new_plan_rejects_disabled_or_rebound_original_workline(session, change):
    task = await issue(session)
    await prepared(session, "task")
    line = await session.get(WorkLine, task.work_line_id)
    for field, value in change.items():
        setattr(line, field, value)
    delta = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R"}])
    if change == {"is_enabled": False}:
        assert isinstance(
            await AutoTaskService().accept_task_plan_delta(ctx(session, "plan", "p", delta), delta),
            Conflict,
        )
    else:
        with pytest.raises(Conflict):
            await AutoTaskService().accept_task_plan_delta(ctx(session, "plan", "p", delta), delta)
    assert task.plan_revision == 0


@pytest.mark.asyncio
async def test_old_plan_requires_original_identity_and_timestamp(session):
    task = await issue(session)
    await prepared(session, "task")
    service = AutoTaskService()
    for revision in (1, 2):
        delta = PlanDelta(
            task_id="task", plan_revision=revision, added_source_racks=[{"rack_id": f"R{revision}"}]
        )
        await service.accept_task_plan_delta(ctx(session, "plan", f"p{revision}", delta), delta)
    old = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R1"}])
    with pytest.raises(Conflict):
        await service.accept_task_plan_delta(ctx(session, "plan", "new", old), old)
    changed = ctx(session, "plan", "p1", old)
    changed = replace(changed, timestamp=101)
    with pytest.raises(Conflict):
        await service.accept_task_plan_delta(changed, old)
    assert task.plan_revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"is_enabled": False}, {"business_code": "smt_manual"}, {"line_code": "OTHER"}]
)
async def test_issued_requires_real_enabled_auto_workline(session, change):
    line = await session.get(WorkLine, 1)
    for field, value in change.items():
        setattr(line, field, value)
    with pytest.raises(Conflict):
        await issue(session)
    assert await session.get(AutoTask, "task") is None
    assert list(await session.scalars(select(AutoTaskInput))) == []


@pytest.mark.asyncio
async def test_task_direction_and_original_issue_are_immutable(session):
    task = await issue(session)
    changed = Issued(
        task_id="task",
        task_type="AUTO",
        business_type="outbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    with pytest.raises(Conflict):
        await AutoTaskService().accept_task_issued(
            ctx(session, "issued", "issue", changed), changed
        )

    wrong = PlanDelta(
        task_id="task", plan_revision=1, added_return_racks=[{"rack_id": "R", "rack_faces": ["A"]}]
    )
    with pytest.raises(Rejected):
        await AutoTaskService().accept_task_plan_delta(ctx(session, "plan", "wrong", wrong), wrong)
    assert task.business_type == "inbound" and task.plan_revision == 0
    assert task.issued_data["business_type"] == "inbound"


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_plan_member_writes_rollback_with_receiver_transaction(session, direction):
    await issue(session, direction=direction)
    await prepared(session, "task")
    await session.commit()
    resources = {"added_bin_source_racks": [{"rack_id": "BIN", "rack_face": ["B", "A"]}]}
    if direction == "inbound":
        resources["added_source_racks"] = [{"rack_id": "SOURCE"}]
        model = AutoInboundSourceMember
    else:
        resources.update(
            target_rack={"rack_id": "TARGET", "rack_face": "A"},
            added_return_racks=[{"rack_id": "SOURCE", "rack_faces": ["B", "A"]}],
        )
        model = AutoOutboundSourceMember
    delta = PlanDelta(task_id="task", plan_revision=1, **resources)
    with pytest.raises(RuntimeError):
        async with session.begin():
            await AutoTaskService().accept_task_plan_delta(ctx(session, "plan", "p", delta), delta)
            await session.flush()
            assert len(list(await session.scalars(select(AutoSupplyMember)))) == 2
            assert list(await session.scalars(select(model)))
            raise RuntimeError("receiver rollback")
    assert (await session.get(AutoTask, "task")).plan_revision == 0
    assert list(await session.scalars(select(AutoSupplyMember))) == []
    assert list(await session.scalars(select(model))) == []
    assert (
        await session.scalar(select(AutoTaskInput).where(AutoTaskInput.operation_id == "p")) is None
    )


@pytest.mark.asyncio
async def test_current_revision_alias_retains_identity_after_later_revision(session):
    await issue(session)
    await prepared(session, "task")
    service = AutoTaskService()
    first = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R1"}])
    await service.accept_task_plan_delta(ctx(session, "plan", "p1", first), first)
    result = await service.accept_task_plan_delta(ctx(session, "plan", "alias", first), first)
    assert result.__class__.__name__ == "Duplicate"
    second = PlanDelta(task_id="task", plan_revision=2, added_source_racks=[{"rack_id": "R2"}])
    await service.accept_task_plan_delta(ctx(session, "plan", "p2", second), second)
    line = await session.get(WorkLine, 1)
    line.is_enabled = False
    result = await service.accept_task_plan_delta(ctx(session, "plan", "alias", first), first)
    assert result.__class__.__name__ == "Duplicate"
    assert len(list(await session.scalars(select(AutoInboundSourceMember)))) == 2


@pytest.mark.asyncio
async def test_auto_host_rejects_manual_issued_branch(session):
    payload = Issued(
        task_id="task",
        task_type="MANUAL",
        business_type="inbound",
        workline_code="L1",
        queue_revision=1,
        dispatch_sequence=1,
    )
    with pytest.raises(Rejected):
        await AutoTaskService().accept_task_issued(
            ctx(session, "issued", "issue", payload), payload
        )
    assert await session.get(AutoTask, "task") is None


@pytest.mark.asyncio
async def test_cancelled_task_only_replays_original_task_cancellation(session):
    await issue(session)
    service = AutoTaskService()
    payload = Cancel(task_id="task", cancel_scope="TASK")
    await service.accept_task_cancel(ctx(session, "cancel", "original", payload), payload)
    result = await service.accept_task_cancel(ctx(session, "cancel", "original", payload), payload)
    assert result.__class__.__name__ == "Duplicate"
    with pytest.raises(Conflict):
        await service.accept_task_cancel(ctx(session, "cancel", "new", payload), payload)


@pytest.mark.asyncio
async def test_member_cancellation_preserves_other_revisions_and_terminal_recovery(session):
    task = await issue(session)
    await prepared(session, "task")
    service = AutoTaskService()
    for revision in (1, 2):
        delta = PlanDelta(
            task_id="task",
            plan_revision=revision,
            added_bin_source_racks=[{"rack_id": "R", "rack_face": ["A"]}],
        )
        await service.accept_task_plan_delta(ctx(session, "plan", f"p{revision}", delta), delta)
    task.status = "COMPLETED"
    task.pending = True
    payload = Cancel(
        task_id="task",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"rack_id": "R", "rack_face": ["A"], "plan_revision": 1}],
    )
    await service.accept_task_cancel(ctx(session, "cancel", "precise", payload), payload)
    assert task.status == "COMPLETED" and task.pending and task.plan_revision == 2
    members = {
        m.plan_revision: m.withdrawn_at_ms for m in await session.scalars(select(AutoSupplyMember))
    }
    assert members == {1: 100, 2: None}


@pytest.mark.asyncio
async def test_prepare_claim_and_blocked_plan_only_rechecks_original(session):
    from types import SimpleNamespace

    from app.businesses._components.sorting_task.service import SortingTask
    from app.integrations.wms_callback import Unavailable
    from app.integrations.wms_request.results import WmsRequestResult

    class Wms:
        calls = 0
        result = None

        async def send(self, session, payload, **kwargs):
            self.calls += 1
            self.identity = kwargs["operation_id"]
            return SimpleNamespace(message_id=42)

        async def get_result(self, session, **kwargs):
            return self.result or WmsRequestResult(**kwargs, availability="UNKNOWN")

    task = await issue(session)
    wms = Wms()
    host = AutoTaskService(wms=wms)
    service = SortingTask("smt_auto", host)
    assert (await service.read_task_owner(session, task_id="task")).business_code == "smt_auto"
    assert await host.prepare_next(session, work_line_id=1, now_ms=200) == "task"
    assert await host.prepare_next(session, work_line_id=1, now_ms=201) is None
    assert wms.calls == 1 and task.status == "PREPARING"
    delta = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R"}])
    context = ctx(session, "sorting.task.plan_delta@v1", "early", delta)
    assert isinstance(await service.accept(context, delta), Unavailable)
    await session.commit()
    assert await host.advance_prepare(session, task_id="task", now_ms=202) == "PREPARING"
    wms.result = WmsRequestResult(
        message_id=42,
        operation="sorting.task.prepare@v1",
        operation_id=wms.identity,
        availability="FOUND",
        response_state="VALID",
        http_status=202,
        response_code="PREPARE_ACCEPTED",
        response_data={},
    )
    await host.advance_prepare(session, task_id="task", now_ms=203)
    assert task.prepare_accepted_at_ms == 203 and task.plan_revision == 0
    assert list(await session.scalars(select(AutoInboundSourceMember))) == []
    result = await service.recheck(context, delta, {"code": "UNAVAILABLE", "data": {}})
    assert result.__class__.__name__ == "Accepted" and result.data == {}
    assert task.plan_revision == 1
    original = await session.scalar(
        select(AutoTaskInput).where(AutoTaskInput.operation_id == "early")
    )
    assert original.first_received_at_ms == 100 and original.disposition == "APPLIED"
    assert (await service.accept(context, delta)).data == {}
    await host.advance_prepare(session, task_id="task", now_ms=999)
    assert task.prepare_accepted_at_ms == 203 and wms.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["sqlite", "postgres"], indirect=True)
async def test_queue_claim_is_ordered_durable_and_rollback_has_no_half_claim(session):
    from types import SimpleNamespace

    class Wms:
        calls = 0

        async def send(self, session, payload, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=self.calls)

    host = AutoTaskService(wms=Wms())
    for task_id, sequence, timestamp, not_before in [
        ("late", 1, 1, 999),
        ("b", 1, 100, None),
        ("a", 1, 100, None),
        ("old", 1, 50, None),
        ("last", 2, 1, None),
    ]:
        payload = Issued(
            task_id=task_id,
            task_type="AUTO",
            business_type="inbound",
            workline_code="L1",
            queue_revision=1,
            dispatch_sequence=sequence,
            **({"not_before": not_before} if not_before is not None else {}),
        )
        await host.accept_task_issued(
            replace(ctx(session, "issued", task_id, payload), timestamp=timestamp), payload
        )
    await session.commit()
    with pytest.raises(RuntimeError):
        async with session.begin():
            assert await host.prepare_next(session, work_line_id=1, now_ms=200) == "old"
            raise RuntimeError("rollback prepare")
    assert (await session.get(AutoTask, "old")).status == "QUEUED"
    assert list(await session.scalars(select(AutoTaskRequest))) == []
    assert await host.prepare_next(session, work_line_id=1, now_ms=200) == "old"
    await session.commit()
    restarted = AutoTaskService(wms=host.wms)
    assert await restarted.prepare_next(session, work_line_id=1, now_ms=201) is None
    first = await session.scalar(select(AutoTaskRequest))
    assert first.task_id == "old" and host.wms.calls == 2
    (await session.get(AutoTask, "old")).status = "COMPLETED"
    assert await restarted.prepare_next(session, work_line_id=1, now_ms=201) == "a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"availability": "UNKNOWN"},
        {"availability": "NOT_FOUND"},
        {"response_state": "INVALID"},
        {"message_id": 999},
        {"operation_id": "other"},
        {"operation": "other"},
        {"http_status": 200},
        {"response_code": "RECEIVED"},
        {"response_data": {"result": "COMPLETED"}},
    ],
)
async def test_prepare_consumes_only_matching_valid_original_result(session, changes):
    from types import SimpleNamespace

    from app.integrations.wms_request.results import WmsRequestResult

    task = await issue(session)
    await prepared(session, "task")
    original = await session.scalar(select(AutoTaskRequest))
    original.unresolved = True
    task.prepare_accepted_at_ms = None
    values = dict(
        message_id=original.message_id,
        operation=original.operation,
        operation_id=original.operation_id,
        availability="FOUND",
        response_state="VALID",
        http_status=202,
        response_code="PREPARE_ACCEPTED",
        response_data={},
    )
    values.update(changes)

    async def result(*args, **kwargs):
        return WmsRequestResult(**values)

    host = AutoTaskService(wms=SimpleNamespace(get_result=result))
    assert await host.advance_prepare(session, task_id="task", now_ms=200) == "PREPARING"
    assert task.prepare_accepted_at_ms is None and original.unresolved
    assert original.response_snapshot is None


@pytest.mark.asyncio
async def test_queue_change_replay_survives_claim_but_new_changes_cannot_mutate(session):
    from app.businesses._components.sorting_task.contracts import QueueChanged

    task = await issue(session)
    service = AutoTaskService()
    payload = QueueChanged(task_id="task", queue_revision=2, dispatch_sequence=8)
    context = ctx(session, "queue", "original", payload)
    await service.accept_task_queue_changed(context, payload)
    task.status = "PREPARING"
    assert (
        await service.accept_task_queue_changed(context, payload)
    ).__class__.__name__ == "Duplicate"
    next_change = payload.model_copy(update={"queue_revision": 3, "dispatch_sequence": 9})
    with pytest.raises(Conflict):
        await service.accept_task_queue_changed(
            ctx(session, "queue", "next", next_change), next_change
        )
    assert task.queue_revision == 2 and task.dispatch_sequence == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["WORKLINE_DISABLED", "MISSING_REVISION"])
async def test_blocked_plan_rechecks_frozen_input_after_prerequisite_restored(session, reason):
    task = await issue(session)
    await prepared(session, "task")
    service = AutoTaskService()
    line = await session.get(WorkLine, 1)
    revision = 2 if reason == "MISSING_REVISION" else 1
    if reason == "WORKLINE_DISABLED":
        line.is_enabled = False
    delta = PlanDelta(
        task_id="task", plan_revision=revision, added_source_racks=[{"rack_id": "LATER"}]
    )
    context = ctx(session, "plan", "blocked", delta)
    result = await service.accept_task_plan_delta(context, delta)
    assert isinstance(result, Conflict)
    refusal = {"code": "CONFLICT", "data": result.data}
    row = await session.scalar(select(AutoTaskInput).where(AutoTaskInput.operation_id == "blocked"))
    assert row.block_reason == reason and row.applied_plan_revision is None
    await session.commit()
    line.is_enabled = True
    if revision == 2:
        first = PlanDelta(
            task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "FIRST"}]
        )
        await service.accept_task_plan_delta(ctx(session, "plan", "first", first), first)
    with pytest.raises(Conflict):
        await service.recheck_task_plan(replace(context, timestamp=101), delta, refusal)
    assert row.disposition == "BLOCKED"
    assert (await service.recheck_task_plan(context, delta, refusal)).data == {}
    assert task.plan_revision == revision and row.disposition == "APPLIED"
    assert row.request_timestamp == row.first_received_at_ms == 100
    assert row.effective_request == delta.model_dump(mode="json")
    assert await service.recheck_task_plan(context, delta, refusal) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        {"business_code": "smt_manual"},
        {"line_code": "OTHER"},
    ],
)
async def test_prepare_claim_cannot_use_disabled_or_rebound_original_line(session, change):
    task = await issue(session)
    line = await session.get(WorkLine, 1)
    for name, value in change.items():
        setattr(line, name, value)
    assert await AutoTaskService().prepare_next(session, work_line_id=1, now_ms=200) is None
    assert task.status == "QUEUED"
    assert list(await session.scalars(select(AutoTaskRequest))) == []


@pytest.mark.asyncio
async def test_queued_plan_is_state_conflict_without_prepare_wait(session):
    await issue(session)
    delta = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R"}])
    with pytest.raises(Conflict) as refusal:
        await AutoTaskService().accept_task_plan_delta(ctx(session, "plan", "p", delta), delta)
    assert refusal.value.data == {"reason_code": "STATE_CONFLICT"}
    assert not list(
        await session.scalars(select(AutoTaskInput).where(AutoTaskInput.operation_id == "p"))
    )


@pytest.mark.asyncio
async def test_accepted_prepare_then_task_cancel_settles_original_pending(session):
    from types import SimpleNamespace

    from app.integrations.wms_request.results import WmsRequestResult

    class Wms:
        calls = 0

        async def send(self, session, payload, **kwargs):
            self.calls += 1
            return SimpleNamespace(message_id=42)

        async def get_result(self, session, **identity):
            return WmsRequestResult(
                **identity,
                availability="FOUND",
                response_state="VALID",
                http_status=202,
                response_code="PREPARE_ACCEPTED",
                response_data={},
            )

    task = await issue(session)
    wms = Wms()
    service = AutoTaskService(wms=wms)
    assert await service.prepare_next(session, work_line_id=1, now_ms=200) == "task"
    await service.advance_prepare(session, task_id="task", now_ms=201)
    original = await session.scalar(select(AutoTaskRequest))
    assert not original.unresolved and task.status == "PREPARING"
    payload = Cancel(task_id="task", cancel_scope="TASK")
    await service.accept_task_cancel(ctx(session, "cancel", "stop", payload), payload)
    await session.commit()
    assert await service.advance_prepare(session, task_id="task", now_ms=202) == "CANCELLED"
    assert not task.pending and not original.unresolved and wms.calls == 1
    assert task.prepare_accepted_at_ms == 201 and original.consumed_at_ms == 201


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        {"business_code": "smt_manual"},
        {"line_code": "OTHER"},
    ],
)
async def test_prepare_refreshes_line_retained_across_transactions(session, change):
    from sqlalchemy import update

    task = await issue(session)
    line = await session.get(WorkLine, 1)
    await session.commit()
    async with async_sessionmaker(session.bind)() as other, other.begin():
        await other.execute(update(WorkLine).where(WorkLine.id == 1).values(**change))
    assert line.is_enabled and line.business_code == "smt_auto" and line.line_code == "L1"
    assert await AutoTaskService().prepare_next(session, work_line_id=1, now_ms=200) is None
    assert task.status == "QUEUED" and list(await session.scalars(select(AutoTaskRequest))) == []


@pytest.mark.asyncio
async def test_cancel_refreshes_task_retained_across_transactions(session):
    from sqlalchemy import update

    task = await issue(session)
    await session.commit()
    async with async_sessionmaker(session.bind)() as other, other.begin():
        await other.execute(
            update(AutoTask)
            .where(AutoTask.task_id == "task")
            .values(status="EXECUTING", plan_revision=1)
        )
    assert task.status == "QUEUED" and task.plan_revision == 0
    payload = Cancel(task_id="task", cancel_scope="TASK")
    with pytest.raises(Conflict):
        await AutoTaskService().accept_task_cancel(ctx(session, "cancel", "stop", payload), payload)
    assert task.status == "EXECUTING" and task.plan_revision == 1


@pytest.mark.asyncio
async def test_successful_plan_refreshes_blocked_input_retained_across_transactions(session):
    task = await issue(session)
    await prepared(session, "task")
    service = AutoTaskService()
    delta = PlanDelta(task_id="task", plan_revision=2, added_source_racks=[{"rack_id": "R2"}])
    context = ctx(session, "plan", "blocked", delta)
    assert isinstance(await service.accept_task_plan_delta(context, delta), Conflict)
    original = await session.scalar(
        select(AutoTaskInput).where(AutoTaskInput.operation_id == "blocked")
    )
    await session.commit()
    async with async_sessionmaker(session.bind)() as other, other.begin():
        first = PlanDelta(task_id="task", plan_revision=1, added_source_racks=[{"rack_id": "R1"}])
        await service.accept_task_plan_delta(ctx(other, "plan", "first", first), first)
        await service.accept_task_plan_delta(ctx(other, "plan", "blocked", delta), delta)
        line = await other.get(WorkLine, 1)
        line.is_enabled = False
    assert original.disposition == "BLOCKED" and task.plan_revision == 0
    result = await service.accept_task_plan_delta(context, delta)
    assert result.__class__.__name__ == "Duplicate" and result.data == {}
    assert original.disposition == "APPLIED" and task.plan_revision == 2
