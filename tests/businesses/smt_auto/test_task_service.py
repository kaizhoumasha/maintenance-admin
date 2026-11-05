import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.businesses._components.sorting_task.contracts import (
    Cancel,
    Issued,
    PlanDelta,
)
from app.businesses.smt_auto.models import (
    AutoInboundSourceMember,
    AutoOutboundSourceMember,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
)
from app.businesses.smt_auto.task_service import AutoTaskService
from app.integrations.wms_callback import CallbackContext, Conflict
from app.models.base import Base
from app.models.workline import WorkLine


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    schema = Base.metadata.schema
    table_schemas = {
        table: table.schema
        for table in (
            AutoTask.__table__,
            AutoTaskInput.__table__,
            AutoSupplyMember.__table__,
            AutoInboundSourceMember.__table__,
            AutoOutboundSourceMember.__table__,
            WorkLine.__table__,
        )
    }
    Base.metadata.schema = None
    for table in table_schemas:
        table.schema = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[
                    AutoTask.__table__,
                    AutoTaskInput.__table__,
                    AutoSupplyMember.__table__,
                    AutoInboundSourceMember.__table__,
                    AutoOutboundSourceMember.__table__,
                    WorkLine.__table__,
                ],
            )
            await conn.execute(
                WorkLine.__table__.insert().values(
                    line_code="L1",
                    line_name="AUTO test line",
                    business_code="smt_auto",
                    is_enabled=True,
                )
            )
    finally:
        Base.metadata.schema = schema
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as value:
        yield value
    await engine.dispose()


def ctx(session, operation, operation_id, payload):
    return CallbackContext(
        session, 1, operation_id, 100, payload.model_dump(mode="json"), operation=operation
    )


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
    delta = PlanDelta(
        task_id="t1",
        plan_revision=1,
        added_source_racks=[{"rack_id": "R1"}],
    )
    result = await service.accept_task_plan_delta(
        ctx(session, "sorting.task.plan_delta@v1", "p1", delta), delta
    )
    assert result.data["plan_revision"] == 1
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
    with pytest.raises(Conflict):
        await service.accept_task_plan_delta(
            ctx(session, "sorting.task.plan_delta@v1", "p3", missing), missing
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
