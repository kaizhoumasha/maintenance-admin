"""Original task completion uses real registered requests and settled public results."""

import json

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import (
    AutoBinWork,
    AutoSupplyMember,
    AutoTask,
    AutoTaskInput,
    AutoTaskRequest,
)
from app.http_client import DeliveryState, HttpResult
from app.integrations import history as history_records
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import ReliableMessage
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from tests.helpers import read_history_body, set_message_bodies


async def complete_result(host, data, *, status=200, code="DECIDED"):
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
    outcome = (
        AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
        if (status, code) in ((200, "DECIDED"), (202, "PREPARE_ACCEPTED"))
        else AttemptOutcome(
            MessageStatus.PENDING,
            result=response,
            verdict=verdict,
            next_attempt_at=host.requests.clock(),
        )
    )
    assert await store.finish(claim, outcome)


async def empty_task(host, *, revision=1):
    async with host.factory.begin() as session:
        work = await session.scalar(select(AutoBinWork))
        await session.delete(work)
        task = await session.get(AutoTask, "t")
        task.plan_revision = revision
        if revision:
            from app.businesses.smt_auto.models import AutoTaskInput

            session.add(
                AutoTaskInput(
                    task_id="t",
                    operation="sorting.task.plan_delta@v1",
                    operation_id="plan",
                    request_timestamp=1,
                    first_received_at_ms=1,
                    effective_digest="digest",
                    contract_version="1",
                    effective_request={},
                    disposition="APPLIED",
                    requested_revision=1,
                    applied_plan_revision=1,
                )
            )


async def completed_supply(host, *, count):
    """Recover a closed feed checkpoint with its original registered WMS decision."""
    import importlib

    from app.businesses import catalog
    from app.businesses._components.bin_roller.composition import COMPONENT
    from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
    from app.businesses.smt_auto.models import AutoSupplyMember, AutoTaskInput
    from app.core.identifiers import uuid7
    from app.integrations.wms_request import operation as registry

    host.entry.COMPONENTS = (host.entry.COMPONENTS[0], COMPONENT)
    catalog._validated_entries = None
    module = importlib.import_module(
        "app.businesses._components.bin_roller.request.bin_inbound_batch_v1"
    )
    if registry.lookup("bin.inbound_batch@v1") is None:
        registry.request_operation("bin.inbound_batch@v1", registry.Endpoint.DECISIONS)(
            module.InboundBatch
        )
    await empty_task(host)
    feed_operation_id = str(uuid7())
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        plan.effective_request = {
            "target_rack": {"rack_id": "TARGET", "rack_face": "90"},
            "added_bin_source_racks": [{"rack_id": "R", "rack_face": ["90"]}],
        }
        session.add(
            AutoSupplyMember(
                task_id="t",
                plan_input_id=plan.id,
                plan_revision=1,
                rack_id="R",
                rack_face="90",
                rack_array_index=0,
                face_array_index=0,
                supply_change_id=plan.operation_id,
            )
        )
        await host.roller.apply_supply_change(
            session, SupplyChange(1, "t", plan.operation_id, (SourceFaceRef(1, "R", "90"),), ())
        )
        message = await host.requests.send(
            session,
            module.InboundBatch(task_id="t", rack_id="R", rack_face="90"),
            operation_id=feed_operation_id,
        )
        repo = host.roller.repository(1)
        face = (await session.execute(select(host.roller.tables["face_work"]))).mappings().one()
        await repo.update(
            session,
            "face_work",
            face["id"],
            phase="RETURN_WINDOW",
            feed_cursor=count,
            feed_operation_id=feed_operation_id,
            feed_message_id=message.message_id,
        )
        await repo.close(session, "face_work", face["id"], "COMPLETED")
    decision = (
        {"result": "RACK_FACE_DONE"}
        if not count
        else {
            "result": "READY",
            "bins": [
                {
                    "bin_code": f"A{i + 1:09}",
                    "source_locator": {
                        "type": "RACK_BIN_SLOT",
                        "rack_id": "R",
                        "rack_face": "90",
                        "slot_id": f"S{i + 1}",
                    },
                }
                for i in range(count)
            ],
        }
    )
    await complete_result(host, decision)


async def terminal_bin(host, flow, *, index=0, now=110):
    work = await host.scan(timestamp=now, bin_code=f"A{index + 1:09}-C")
    await complete_result(host, {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=now + 10
        )
        assert (
            await flow.release_bin(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=now + 20
            )
            == "RELEASING"
        )
        work = await session.get(AutoBinWork, work.id)
        return work


@pytest.mark.parametrize(
    ("cancelled", "scan3_code", "scan3_at", "expected"),
    [
        (True, "A000000002-B", 230, "CONFIRMING"),
        (False, "A000000002-B", 230, "WAITING"),
        (True, "A000000003-B", 230, "WAITING"),
        (True, "A000000002-B", 205, "WAITING"),
    ],
)
async def test_completion_accounts_for_original_cancelled_bin_at_scan3(
    s5_roller, cancelled, scan3_code, scan3_at, expected
):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=2)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    await terminal_bin(host, flow)
    await host.scan(point="SCAN1", timestamp=200, bin_code="A000000002-B")
    if cancelled:
        async with host.factory.begin() as session:
            cancel = AutoTaskInput(
                task_id="t",
                operation="sorting.task.cancel@v1",
                operation_id="cancel",
                request_timestamp=210,
                first_received_at_ms=210,
                effective_digest="cancel",
                contract_version="1",
                effective_request={
                    "cancel_scope": "PLAN_MEMBERS",
                    "bin_source_racks": [{"plan_revision": 1, "rack_id": "R", "rack_face": ["90"]}],
                },
                disposition="APPLIED",
            )
            session.add(cancel)
            await session.flush()
            member = await session.scalar(select(AutoSupplyMember))
            member.withdrawn_at_ms, member.withdrawn_by_input_id = 210, cancel.id
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=220) == "WAITING"
    await host.scan(point="SCAN3", timestamp=scan3_at, bin_code=scan3_code)
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=240) == expected
        assert await session.scalar(select(func.count()).select_from(AutoBinWork)) == 1
    if expected == "CONFIRMING":
        await complete_result(host, {"result": "COMPLETED"})
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=250) == "COMPLETED"


@pytest.mark.parametrize("count", [1, 2])
async def test_successful_supply_waits_for_bins_to_enter_production(s5_roller, count):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=count)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "WAITING"
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        assert not await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
    for index in range(count):
        now = 110 + 100 * index
        work = await terminal_bin(host, flow, index=index, now=now)
        command_code = work.release_command_code
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=now + 40) == (
                "CONFIRMING" if index + 1 == count else "WAITING"
            )
        # A late tail Result must not change the already-saved confirmation basis.
        if index + 1 < count:
            assert (await host.result(command_code)).status_code == 200
            async with host.factory.begin() as session:
                assert (
                    await flow.consume_release(
                        session, work_line_id=1, arrival_id=work.arrival_id, now_ms=now + 50
                    )
                    == "RELEASED"
                )
    await complete_result(host, {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=400) == "COMPLETED"
        task = await session.get(AutoTask, "t")
        assert await flow.retention_basis(session, task, now_ms=401) is None
        assert await flow.original_inprocess(session, work_line_id=1, bin_code=work.bin_code)
    assert (await host.result(command_code)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.consume_release(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=410
            )
            == "RELEASED"
        )
        task = await session.get(AutoTask, "t")
        assert await flow.retention_basis(session, task, now_ms=420) == task.confirmation_basis
        assert not await flow.original_inprocess(session, work_line_id=1, bin_code=work.bin_code)


@pytest.mark.parametrize("missing", ["decision", "command", "owner", "production"])
async def test_completion_requires_original_release_intent(s5_roller, missing):
    from sqlalchemy import update

    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=1)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    work = await terminal_bin(host, flow)
    async with host.factory.begin() as session:
        if missing == "decision":
            (await session.get(AutoBinWork, work.id)).release_decided_at_ms = None
        else:
            table = host.roller.tables["bin_passage"]
            key = {
                "command": "release_command_code",
                "owner": "scope_id",
                "production": "production_record_id",
            }[missing]
            await session.execute(update(table).values({key: "OTHER"}))
        assert await flow.advance(session, task_id="t", now_ms=200) == "WAITING"


async def test_saved_result_based_confirmation_remains_recoverable(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=1)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    work = await terminal_bin(host, flow)
    assert (await host.result(work.release_command_code)).status_code == 200
    async with host.factory.begin() as session:
        await flow.consume_release(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=150)
        assert await flow.advance(session, task_id="t", now_ms=160) == "CONFIRMING"
        task = await session.get(AutoTask, "t")
        saved = await session.get(AutoBinWork, work.id)
        task.confirmation_basis = {
            **task.confirmation_basis,
            "bins": [[saved.id, saved.release_result_ref]],
        }
    await complete_result(host, {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=200) == "COMPLETED"
        task = await session.get(AutoTask, "t")
        assert await flow.retention_basis(session, task, now_ms=210) == task.confirmation_basis


@pytest.mark.parametrize("cancelled", [False, True])
async def test_inbound_retention_waits_for_original_tail_result(s5_roller, cancelled):
    from types import SimpleNamespace

    from app.businesses._components.sorting_task.contracts import Cancel
    from app.businesses._components.sorting_task.request.sorting_task_prepare_v1 import Prepare
    from app.businesses.smt_auto.archive import RETENTION_MS, Archive
    from app.businesses.smt_auto.rack_ports import AutoRackPorts
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.core.identifiers import uuid7

    host = s5_roller
    await empty_task(host, revision=0 if cancelled else 1)
    service = AutoTaskService(wms=host.requests)
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "t")
        task.business_type = "inbound"
        task.status = "PREPARING" if cancelled else "EXECUTING"
        prepare_id = str(uuid7())
        message = await host.requests.send(
            session, Prepare(task_id="t", workline_code="LINE"), operation_id=prepare_id
        )
        prepare = await session.scalar(select(AutoTaskRequest))
        prepare.operation_id = prepare_id
        prepare.message_id, prepare.unresolved = message.message_id, True
    await complete_result(host, {}, status=202, code="PREPARE_ACCEPTED")
    async with host.factory.begin() as session:
        await service.advance_prepare(session, task_id="t", now_ms=10)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    work = await terminal_bin(host, flow)
    if cancelled:
        async with host.factory.begin() as session:
            await service.accept_task_cancel(
                SimpleNamespace(
                    session=session,
                    operation="sorting.task.cancel@v1",
                    operation_id="cancel",
                    timestamp=160,
                    parsed={},
                ),
                Cancel(task_id="t", cancel_scope="TASK"),
            )
            await service.advance_prepare(session, task_id="t", now_ms=160)
    else:
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=160) == "CONFIRMING"
        await complete_result(host, {"result": "COMPLETED"})
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=170) == "COMPLETED"
    archive = Archive(flow, AutoRackPorts(host.dispatch))
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "t")
        late = task.closed_at_ms + RETENTION_MS + 100
        assert not await archive.clean(session, task_id="t", now_ms=late)
    assert (await host.result(work.release_command_code)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.consume_release(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=late
            )
            == "RELEASED"
        )
        assert not await archive.clean(session, task_id="t", now_ms=late + RETENTION_MS - 1)
        assert await archive.clean(session, task_id="t", now_ms=late + RETENTION_MS)


async def test_explicit_empty_supply_can_confirm_without_production(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=0)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"


async def test_saved_original_supply_confirmation_can_complete_and_retain(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=0)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
        progress = await host.roller.get_supply_progress(session, work_line_id=1, task_id="t")
        task = await session.get(AutoTask, "t")
        task.confirmation_basis = {
            "plan_revision": 1,
            "inputs": ["plan"],
            "bins": [],
            "supply": [[1, "R", "90", list(progress[0].original_ids)]],
        }
    await complete_result(host, {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=200) == "COMPLETED"
        task = await session.get(AutoTask, "t")
        assert await flow.retention_basis(session, task, now_ms=300) is not None


@pytest.mark.parametrize("confirmation_sent", [False, True])
async def test_missing_original_supply_decision_cannot_close_task(s5_roller, confirmation_sent):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await completed_supply(host, count=0)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    if confirmation_sent:
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
        await complete_result(host, {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        feed = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.operation == "bin.inbound_batch@v1")
        )
        await set_message_bodies(session, feed, response_raw=None)
        assert await flow.advance(session, task_id="t", now_ms=200) == (
            "CONFLICT" if confirmation_sent else "WAITING"
        )
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"


async def test_original_completion_recovery_uses_one_outbox_and_consumes_once(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, roller=None, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        first = await flow.advance(session, task_id="t", now_ms=20)
        second = await flow.advance(session, task_id="t", now_ms=99)
        assert first == second == "CONFIRMING"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        request = await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        assert request.request_data == {"task_id": "t", "last_applied_plan_revision": 1}
    await complete_result(direction_host, {"result": "COMPLETED"})
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "COMPLETED"
        assert await flow.advance(session, task_id="t", now_ms=200) == "COMPLETED"
        assert (await session.get(AutoTask, "t")).closed_at_ms == 100


@pytest.mark.parametrize(
    "data",
    [
        {"result": "PLAN_REVISION_STALE", "current_plan_revision": 1},
        {"result": "COMPLETED", "retry_after_ms": 1000},
        {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": None},
    ],
)
async def test_invalid_completion_never_consumes_or_releases_confirmation_hold(
    direction_host, data
):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
    await complete_result(direction_host, data)
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
        request = await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        assert request.unresolved and request.consumed_at_ms is None


async def test_old_completed_preserves_new_plan_and_original_response(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
    await complete_result(direction_host, {"result": "COMPLETED"})
    async with direction_host.factory.begin() as session:
        (await session.get(AutoTask, "t")).plan_revision = 2
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFLICT"
        request = await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        assert request.response_snapshot["data"] == {"result": "COMPLETED"}
        assert request.unresolved
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"


async def test_zero_plan_requires_original_explicit_elapsed_deadline(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host, revision=0)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=10000) == "WAITING"
        task = await session.get(AutoTask, "t")
        task.first_plan_wait_ms = 100
        task.first_plan_deadline_ms = 101
        assert await flow.advance(session, task_id="t", now_ms=100) == "WAITING"
        assert await flow.advance(session, task_id="t", now_ms=101) == "CONFIRMING"


async def test_nonterminal_confirmation_resumes_original_held_first_plan_once(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.businesses.smt_auto.work_plans import WorkPlans

    await empty_task(direction_host)
    plans = WorkPlans(direction_host.requests, direction_host.dispatch)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch, work_plans=plans)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
        session.add(
            AutoBinWork(
                work_line_id=1,
                arrival_id="held",
                device_code="SCAN2",
                raw_bin_code="A000000001-C",
                bin_code="A000000001",
                scanned_at=22,
                task_id="t",
                business_type="outbound",
                association="TASK",
                production_hold_reason="CONFIRMATION_PENDING",
            )
        )
    await complete_result(
        direction_host, {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 1000}
    )
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "BUSINESS_IN_PROGRESS"
        await flow.resume_held(session, task_id="t", now_ms=101)
        from app.businesses.smt_auto.models import AutoOutboundDecisionRound

        rounds = tuple(await session.scalars(select(AutoOutboundDecisionRound)))
        assert len(rounds) == 1 and rounds[0].request_data["scanned_at"] == 22
        work = await session.scalar(select(AutoBinWork))
        assert work.task_id == "t" and work.production_hold_reason is None


async def test_in_progress_wait_and_stale_use_original_requested_version(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
    await complete_result(
        direction_host, {"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 1000}
    )
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "BUSINESS_IN_PROGRESS"
        assert await flow.advance(session, task_id="t", now_ms=1099) == "WAITING"
        assert await flow.advance(session, task_id="t", now_ms=1100) == "CONFIRMING"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 2


@pytest.mark.parametrize("http_status,code", [(503, "UNAVAILABLE"), (200, "ACCEPTED")])
async def test_unknown_response_recovers_original_message_bytes(direction_host, http_status, code):
    from app.businesses.smt_auto.task_flow import TaskFlow

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
        message = await session.scalar(select(ReliableMessage))
        identity = (
            message.id,
            message.operation_id,
            await read_history_body(session, (message).request_record_id),
        )
    await complete_result(direction_host, {}, status=http_status, code=code)
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
        message = await session.scalar(select(ReliableMessage))
        assert (
            message.id,
            message.operation_id,
            await read_history_body(session, (message).request_record_id),
        ) == identity
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1


async def test_supply_requires_actual_complete_original_progress(s5_roller):
    from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
    from app.businesses.smt_auto.models import AutoSupplyMember, AutoTaskInput
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    await empty_task(host)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        session.add(
            AutoSupplyMember(
                task_id="t",
                plan_input_id=plan.id,
                plan_revision=1,
                rack_id="R",
                rack_face="90",
                rack_array_index=0,
                face_array_index=0,
                supply_change_id="supply",
            )
        )
        await session.flush()
        assert await host.roller.get_supply_progress(session, work_line_id=1, task_id="t") == ()
        assert await flow.advance(session, task_id="t", now_ms=100) == "WAITING"
        await host.roller.apply_supply_change(
            session, SupplyChange(1, "t", "supply", (SourceFaceRef(1, "R", "90"),), ())
        )
        progress = await host.roller.get_supply_progress(session, work_line_id=1, task_id="t")
        assert len(progress) == 1 and progress[0].pending and not progress[0].feed_complete
        assert await flow.advance(session, task_id="t", now_ms=100) == "WAITING"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_stale_missing_plan_keeps_original_arrival_held_until_recovery(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.businesses.smt_auto.work_plans import WorkPlans

    await empty_task(direction_host)
    plans = WorkPlans(direction_host.requests, direction_host.dispatch)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch, work_plans=plans)
    async with direction_host.factory.begin() as session:
        await flow.advance(session, task_id="t", now_ms=20)
        session.add(
            AutoBinWork(
                work_line_id=1,
                arrival_id="held",
                device_code="SCAN2",
                raw_bin_code="A000000001-C",
                bin_code="A000000001",
                scanned_at=22,
                task_id="t",
                business_type="outbound",
                association="TASK",
                production_hold_reason="CONFIRMATION_PENDING",
            )
        )
    await complete_result(
        direction_host, {"result": "PLAN_REVISION_STALE", "current_plan_revision": 3}
    )
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "PLAN_REVISION_STALE"
        work = await session.scalar(select(AutoBinWork))
        assert work.production_hold_reason == "CONFIRMATION_PENDING"
        (await session.get(AutoTask, "t")).plan_revision = 3
        await flow.advance(session, task_id="t", now_ms=101)
        assert work.production_hold_reason is None
        from app.businesses.smt_auto.models import AutoOutboundDecisionRound

        assert (
            await session.scalar(select(func.count()).select_from(AutoOutboundDecisionRound)) == 1
        )


async def test_retention_rechecks_original_completion_without_current_line(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.models import WorkLine

    await empty_task(direction_host)
    flow = TaskFlow(direction_host.requests, dispatch=direction_host.dispatch)
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=20) == "CONFIRMING"
    await complete_result(direction_host, {"result": "COMPLETED"})
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "COMPLETED"
        task = await session.get(AutoTask, "t")
        assert task.direct_obligations_closed_at_ms == 100
        line = await session.get(WorkLine, 1)
        line.business_code = "other"
        assert await flow.retention_basis(session, task, now_ms=200) == task.confirmation_basis
        original = await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        original.consumed_at_ms = None
        assert await flow.retention_basis(session, task, now_ms=200) is None


async def test_original_completion_is_consumed_after_workline_disable(direction_host):
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.models import WorkLine

    host = direction_host
    await empty_task(host)
    flow = TaskFlow(host.requests, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=20) == "CONFIRMING"
    await complete_result(host, {"result": "COMPLETED"})
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.is_enabled = False
        assert await flow.advance(session, task_id="t", now_ms=30, allow_new=False) == "COMPLETED"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1


async def test_missing_supply_rows_cannot_erase_an_applied_original_plan(direction_host):
    from app.businesses.smt_auto.models import AutoTaskInput
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = direction_host
    await empty_task(host)
    flow = TaskFlow(host.requests, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        plan.effective_request = {
            "added_bin_source_racks": [{"rack_id": "ORIGINAL", "rack_face": ["90"]}]
        }
        await session.flush()
        assert await flow.advance(session, task_id="t", now_ms=30) == "WAITING"
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0
