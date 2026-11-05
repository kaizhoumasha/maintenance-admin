"""AUTO admission and original supply handoff through public component ports."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.businesses._components.bin_roller.ports import Scan2Arrival, SourceFaceRef
from app.businesses._components.bin_roller.service import BinRoller
from app.businesses._components.rack_dispatch.service import RackDispatch
from app.businesses.smt_auto.models import TABLES, AutoBinWork, AutoTask, AutoTaskRequest
from app.businesses.smt_auto.roller_ports import RollerPorts
from app.integrations.ecs_command.service import CommandOwner
from app.models import WorkLine
from app.models.base import Base
from tests.helpers import copy_roller_tables


@pytest.fixture
async def host():
    metadata = MetaData(schema=Base.metadata.schema)
    for table in (*TABLES.values(), WorkLine.__table__):
        table.to_metadata(metadata)
    roller_tables = copy_roller_tables(metadata, "auto_roller_test_")
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        execution_options={"schema_translate_map": {Base.metadata.schema: None}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ports = RollerPorts()
    roller = BinRoller(
        CommandOwner("smt_auto"),
        SimpleNamespace(),
        ports,
        rack_dispatch=RackDispatch("smt_auto", None),
        tables=roller_tables,
    )
    async with factory.begin() as session:
        for line, direction in ((1, "inbound"), (2, "outbound")):
            session.add(
                WorkLine(
                    id=line,
                    line_code=f"L{line}",
                    line_name="AUTO",
                    business_code="smt_auto",
                    is_enabled=True,
                )
            )
            session.add(
                AutoTask(
                    task_id=f"t{line}",
                    business_type=direction,
                    work_line_id=line,
                    workline_code=f"L{line}",
                    issued_operation_id=f"i{line}",
                    issued_timestamp=1,
                    issued_data={},
                    status="PREPARING",
                    dispatch_sequence=1,
                )
            )
    yield SimpleNamespace(factory=factory, ports=ports, roller=roller)
    await engine.dispose()


async def prepare(session, line):
    task = await session.get(AutoTask, f"t{line}")
    task.prepare_accepted_at_ms = 1
    session.add(
        AutoTaskRequest(
            task_id=task.task_id,
            purpose="PREPARE",
            operation="sorting.task.prepare@v1",
            operation_id=f"p{line}",
            message_id=line,
            request_data={"task_id": task.task_id, "workline_code": task.workline_code},
            unresolved=False,
            response_snapshot={"http_status": 202, "code": "PREPARE_ACCEPTED", "data": {}},
            consumed_at_ms=1,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_preparing_arrival_remains_original_and_does_not_activate_after_prepare(host):
    async with host.factory.begin() as session:
        arrival = Scan2Arrival("arrival", 1, "SCAN2", "A000000001-C", "A000000001", 1)
        current = await host.ports.resolve_scan2_task(session, work_line_id=1)
        assert current.task_id == "t1" and not current.is_valid
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        await prepare(session, 1)
        assert (await host.ports.read_current_task(session, work_line_id=1)).is_valid
        await host.ports.accept_scan2(
            SimpleNamespace(session=session), replace(arrival, device_code="OTHER", scanned_at=99)
        )
        work = await session.scalar(select(AutoBinWork))
        assert (work.task_id, work.business_type, work.device_code, work.scanned_at) == (
            "t1",
            "inbound",
            "SCAN2",
            1,
        )
        assert work.production_hold_reason == "CONTEXT_NOT_READY" and not work.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("line,direction", [(1, "inbound"), (2, "outbound")])
async def test_valid_zero_plan_accepts_without_supply_or_scan1(host, line, direction):
    async with host.factory.begin() as session:
        await prepare(session, line)
        arrival = Scan2Arrival("arrival", line, "SCAN2", "A000000001-C", "A000000001", 1)
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        work = await session.scalar(select(AutoBinWork))
        assert (work.task_id, work.business_type, work.association, work.pending) == (
            f"t{line}",
            direction,
            "TASK",
            True,
        )
        assert work.production_hold_reason is None
        successor = await host.ports.read_successor(session, work_line_id=line)
        assert successor.state == "AVAILABLE"
        assert successor.evidence["prepare_operation_id"] == f"p{line}"
        task = await session.get(AutoTask, f"t{line}")
        task.status = "CANCELLED"
        (await session.get(WorkLine, line)).is_enabled = False
        await session.flush()
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        assert work.task_id == f"t{line}" and work.business_type == direction
        assert not (await host.ports.read_current_task(session, work_line_id=line)).is_valid


@pytest.mark.asyncio
async def test_missing_successor_is_unknown_and_confirmation_hold_is_distinct(host):
    async with host.factory.begin() as session:
        assert (await host.ports.read_successor(session, work_line_id=999)).state == "UNKNOWN"
        await prepare(session, 1)
        session.add(
            AutoTaskRequest(
                task_id="t1",
                purpose="COMPLETION",
                operation="sorting.task.completion_confirm@v1",
                operation_id="confirm",
                message_id=3,
                request_data={},
                unresolved=True,
            )
        )
        await session.flush()
        await host.ports.accept_scan2(
            SimpleNamespace(session=session),
            Scan2Arrival("arrival", 1, "SCAN2", "A000000001-C", "A000000001", 1),
        )
        work = await session.scalar(select(AutoBinWork))
        assert work.production_hold_reason == "CONFIRMATION_PENDING" and work.pending


@pytest.mark.asyncio
async def test_arrival_rolls_back_with_receiver(host):
    async with host.factory() as session:
        await host.ports.accept_scan2(
            SimpleNamespace(session=session), Scan2Arrival("arrival", 1, "SCAN2", "bad", None, 1)
        )
        await session.rollback()
    async with host.factory() as session:
        assert await session.scalar(select(AutoBinWork)) is None


@pytest.mark.asyncio
async def test_receiver_plan_hands_original_order_and_withdrawal_to_real_roller(host):
    from app.businesses._components.sorting_task.contracts import Cancel, PlanDelta
    from app.businesses.smt_auto.models import AutoTaskInput
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    service = AutoTaskService(roller=host.roller)

    def context(session, name, identity, payload):
        return CallbackContext(
            session,
            1,
            identity,
            100,
            payload.model_dump(mode="json"),
            operation=f"sorting.task.{name}@v1",
        )

    async with host.factory.begin() as session:
        await prepare(session, 1)
        for revision in (1, 2):
            delta = PlanDelta(
                task_id="t1",
                plan_revision=revision,
                added_bin_source_racks=[{"rack_id": "R1", "rack_face": ["B", "A"]}],
                added_source_racks=[{"rack_id": f"S{revision}"}],
            )
            await service.accept_task_plan_delta(
                context(session, "plan_delta", f"delta{revision}", delta), delta
            )
            await service.accept_task_plan_delta(
                context(session, "plan_delta", f"alias{revision}", delta), delta
            )
        sources = await host.ports.read_supply_order(session, work_line_id=1, task_id="t1")
        assert sources == tuple(
            SourceFaceRef(revision, "R1", face) for revision in (1, 2) for face in ("B", "A")
        )
        progress = await host.roller.get_supply_progress(session, work_line_id=1, task_id="t1")
        assert len(progress) == 4 and all(p.pending for p in progress)
        assert await host.roller.get_supply_progress(session, work_line_id=2, task_id="t1") == ()
        cancellation = Cancel(
            task_id="t1",
            cancel_scope="PLAN_MEMBERS",
            bin_source_racks=[{"plan_revision": 1, "rack_id": "R1", "rack_face": ["A"]}],
        )
        await service.accept_task_cancel(
            context(session, "cancel", "withdraw", cancellation), cancellation
        )
        await service.accept_task_cancel(
            context(session, "cancel", "withdraw", cancellation), cancellation
        )
        progress = await host.roller.get_supply_progress(session, work_line_id=1, task_id="t1")
        assert sum(p.pending for p in progress) == 3
        changes = tuple(
            await session.scalars(
                select(AutoTaskInput).where(AutoTaskInput.supply_change_id.is_not(None))
            )
        )
        assert [row.supply_change_id for row in changes] == ["delta1", "delta2", "withdraw"]
        assert changes[-1].supply_change["withdrawn"] == [
            {"plan_revision": 1, "rack_id": "R1", "rack_face": "A"}
        ]


@pytest.mark.asyncio
async def test_receiver_supply_and_members_rollback_together(host):
    from app.businesses._components.sorting_task.contracts import PlanDelta
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    async with host.factory() as session:
        await prepare(session, 1)
        delta = PlanDelta(
            task_id="t1",
            plan_revision=1,
            added_bin_source_racks=[{"rack_id": "R", "rack_face": ["A"]}],
        )
        ctx = CallbackContext(
            session,
            1,
            "delta",
            100,
            delta.model_dump(mode="json"),
            operation="sorting.task.plan_delta@v1",
        )
        await AutoTaskService(roller=host.roller).accept_task_plan_delta(ctx, delta)
        await session.rollback()
    async with host.factory() as session:
        assert await host.roller.get_supply_progress(session, work_line_id=1, task_id="t1") == ()
        assert await host.ports.read_supply_order(session, work_line_id=1, task_id="t1") == ()


@pytest.mark.asyncio
async def test_prepare_requires_original_valid_response_and_zero_time_is_legal(host):
    async with host.factory.begin() as session:
        await prepare(session, 1)
        request = await session.scalar(select(AutoTaskRequest))
        request.response_snapshot = {"http_status": 200, "code": "NOT_ACCEPTED", "data": {}}
        await session.flush()
        assert not (await host.ports.read_current_task(session, work_line_id=1)).is_valid
        request.response_snapshot = {"http_status": 202, "code": "PREPARE_ACCEPTED", "data": {}}
        await session.flush()
        await host.ports.accept_scan2(
            SimpleNamespace(session=session),
            Scan2Arrival("zero", 1, "SCAN2", "A000000001-C", "A000000001", 0),
        )
        work = await session.scalar(select(AutoBinWork))
        assert work.production_hold_reason is None and work.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"is_enabled": False}, {"line_code": "changed"}, {"business_code": "smt_manual"}]
)
async def test_configuration_changes_block_new_production_preserving_task_identity(host, change):
    async with host.factory.begin() as session:
        await prepare(session, 1)
        line = await session.get(WorkLine, 1)
        for key, value in change.items():
            setattr(line, key, value)
        await session.flush()
        current = await host.ports.read_current_task(session, work_line_id=1)
        assert current.task_id == "t1" and not current.is_valid
        await host.ports.accept_scan2(
            SimpleNamespace(session=session),
            Scan2Arrival("new", 1, "SCAN2", "A000000001-C", "A000000001", 1),
        )
        work = await session.scalar(select(AutoBinWork))
        assert work.production_hold_reason == "CONTEXT_NOT_READY" and work.task_id == "t1"


@pytest.mark.asyncio
async def test_task_coverage_and_supply_never_cross_lines(host):
    from app.businesses._components.rack_dispatch.contracts import PointRef

    async with host.factory.begin() as session:
        point = PointRef("smt_auto", 1, "job", "rack", "FIVE_RACK")
        coverage = await host.ports.read_task_coverage(session, point=point, task_id="t1")
        assert coverage.anchor.key == ("t1",) and coverage.rows == ()
        assert await host.ports.read_task_coverage(session, point=point, task_id="t2") is None
        assert (
            await host.ports.read_task_coverage(
                session, point=replace(point, owner_domain="smt_manual"), task_id="t1"
            )
            is None
        )
        assert await host.ports.read_supply_order(session, work_line_id=2, task_id="t1") == ()


@pytest.mark.asyncio
async def test_terminal_and_queued_tasks_are_not_current_or_proof_of_no_successor(host):
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "t1")
        for status in ("QUEUED", "CANCELLED", "COMPLETED"):
            task.status = status
            await session.flush()
            current = await host.ports.read_current_task(session, work_line_id=1)
            assert current.task_id is None and not current.is_valid
            assert (await host.ports.read_successor(session, work_line_id=1)).state != "NONE"
        await host.ports.accept_scan2(
            SimpleNamespace(session=session),
            Scan2Arrival("no-task", 1, "SCAN2", "A000000001-C", "A000000001", 1),
        )
        work = await session.scalar(select(AutoBinWork))
        assert work.association == "NO_TASK" and work.pending
        assert work.release_command_code is None and work.terminal_at_ms is None


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["wrong_operation", "ambiguous"])
async def test_prepare_association_must_be_unique_and_original_before_production(host, evidence):
    async with host.factory.begin() as session:
        await prepare(session, 1)
        request = await session.scalar(select(AutoTaskRequest))
        if evidence == "wrong_operation":
            request.operation = "sorting.task.completion_confirm@v1"
        else:
            session.add(
                AutoTaskRequest(
                    task_id="t1",
                    purpose="PREPARE",
                    operation="sorting.task.prepare@v1",
                    operation_id="other-prepare",
                    message_id=99,
                    request_data=request.request_data,
                    unresolved=False,
                    consumed_at_ms=1,
                    response_snapshot=request.response_snapshot,
                )
            )
        await session.flush()
        current = await host.ports.read_current_task(session, work_line_id=1)
        assert current.task_id == "t1" and not current.is_valid
        successor = await host.ports.read_successor(session, work_line_id=1)
        assert successor.state == "CHECKING" and successor.evidence["task_id"] == "t1"
        arrival = Scan2Arrival("held", 1, "SCAN2", "A000000001-C", "A000000001", 1)
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        work = await session.scalar(select(AutoBinWork))
        assert work.task_id == "t1" and work.business_type == "inbound"
        assert work.production_hold_reason == "CONTEXT_NOT_READY" and not work.pending


async def test_fresh_arrival_cannot_bypass_original_terminal_task_responsibility(host):
    async with host.factory.begin() as session:
        await prepare(session, 1)
        old = await session.get(AutoTask, "t1")
        old.status = "COMPLETED"
        session.add(
            AutoBinWork(
                work_line_id=1,
                arrival_id="old",
                device_code="SCAN2",
                raw_bin_code="A000000001-C",
                bin_code="A000000001",
                scanned_at=1,
                task_id="t1",
                business_type="inbound",
                association="TASK",
                pending=True,
            )
        )
        await session.flush()
        # Readable NO_TASK is not proof that the original BIN's physical obligations ended.
        arrival = Scan2Arrival("fresh", 1, "SCAN2", "A000000001-C", "A000000001", 99)
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        work = await session.scalar(select(AutoBinWork).where(AutoBinWork.arrival_id == "fresh"))
        assert work.task_id == "t1" and work.business_type == "inbound"
        assert work.production_hold_reason == "ORIGINAL_INPROCESS"
        assert work.exception_basis["original_arrival_ids"] == ["old"]


async def test_new_current_task_never_steals_same_bin_original_responsibility(host):
    async with host.factory.begin() as session:
        old = await session.get(AutoTask, "t1")
        old.status = "CANCELLED"
        session.add(
            AutoBinWork(
                work_line_id=1,
                arrival_id="old",
                device_code="SCAN2",
                raw_bin_code="A000000001-C",
                bin_code="A000000001",
                scanned_at=1,
                task_id="t1",
                business_type="inbound",
                association="TASK",
                pending=True,
            )
        )
        session.add(
            AutoTask(
                task_id="new",
                business_type="outbound",
                work_line_id=1,
                workline_code="L1",
                issued_operation_id="new-i",
                issued_timestamp=2,
                issued_data={},
                status="EXECUTING",
                dispatch_sequence=2,
            )
        )
        await session.flush()
        arrival = Scan2Arrival("fresh", 1, "SCAN2", "A000000001-C", "A000000001", 99)
        await host.ports.accept_scan2(SimpleNamespace(session=session), arrival)
        work = await session.scalar(select(AutoBinWork).where(AutoBinWork.arrival_id == "fresh"))
        assert work.task_id == "t1" and work.production_hold_reason == "ORIGINAL_INPROCESS"
        assert work.exception_basis["observed_current_task_id"] == "new"
