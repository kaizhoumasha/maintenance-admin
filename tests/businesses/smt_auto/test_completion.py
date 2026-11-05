"""Original task completion uses real registered requests and settled public results."""

import json

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import AutoBinWork, AutoTask, AutoTaskRequest
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import ReliableMessage
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus


async def complete_result(host, data, *, status=200, code="DECIDED"):
    store = MessageStore(host.factory)
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
        if status == 200 and code == "DECIDED"
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
        identity = message.id, message.operation_id, message.request_raw
    await complete_result(direction_host, {}, status=http_status, code=code)
    async with direction_host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
        message = await session.scalar(select(ReliableMessage))
        assert (message.id, message.operation_id, message.request_raw) == identity
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
