"""SCAN2 release is original public ECS success, never an ACK or a JSON marker."""

import json

import pytest
from sqlalchemy import func, select

from app.businesses.smt_auto.models import AutoBinWork, AutoTask, AutoTaskRequest
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_request import ADAPTERS, DECISIONS_PROTOCOL
from app.models import EcsCommand, ReliableMessage, WorkLine
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.types import MessageStatus
from app.tasks.reliable_tasks import run_cleanup


async def no_work(host, data=None):
    store = MessageStore(host.factory)
    claims = await store.claim(1, host.requests.outbox.targets)
    assert len(claims) == 1
    claim = claims[0]
    response = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        200,
        (),
        json.dumps(
            dict(
                operation_id=claim.message.operation_id,
                timestamp=20,
                code="DECIDED",
                data={"result": "NO_WORK"} if data is None else data,
            )
        ).encode(),
    )
    verdict = ADAPTERS[DECISIONS_PROTOCOL].classify_response(claim.message, response)
    assert await store.finish(
        claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    )


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
@pytest.mark.parametrize("line", [1, 2])
async def test_original_no_work_releases_only_after_actual_public_success(
    s5_roller, direction, status, line
):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
        task = await session.get(AutoTask, "t")
        task.business_type = direction
        task.first_plan_wait_ms, task.first_plan_deadline_ms = 10, 11
        if line == 2:
            session.add(
                WorkLine(
                    id=2,
                    line_code="LINE2",
                    line_name="line2",
                    business_code="smt_auto",
                    is_enabled=True,
                )
            )
            task.work_line_id, task.workline_code = 2, "LINE2"
            prepare = await session.scalar(select(AutoTaskRequest))
            prepare.request_data = {"task_id": "t", "workline_code": "LINE2"}
        host.line_id = line
    work = await host.scan()
    assert work is not None
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        assert (
            await flow.release_bin(
                session, work_line_id=line, arrival_id=work.arrival_id, now_ms=20
            )
            == "WAITING"
        )
    await no_work(host)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=line, arrival_id=work.arrival_id, now_ms=30
        )
        assert (
            await flow.release_bin(
                session, work_line_id=line, arrival_id=work.arrival_id, now_ms=31
            )
            == "RELEASING"
        )
        assert (
            await flow.release_bin(
                session, work_line_id=line, arrival_id=work.arrival_id, now_ms=32
            )
            == "RELEASING"
        )
        saved = await session.get(AutoBinWork, work.id)
        command = saved.release_command_code
        assert command and saved.release_consumed_at_ms is None
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
    response = await host.result(command, status=status)
    assert response.status_code == 200
    async with host.factory.begin() as session:
        assert await flow.release_bin(
            session, work_line_id=line, arrival_id=work.arrival_id, now_ms=100
        ) == ("RELEASED" if status == "SUCCESS" else "RELEASING")
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1
        saved = await session.get(AutoBinWork, work.id)
        assert (saved.release_consumed_at_ms is not None) == (status == "SUCCESS")
        assert (
            await flow.release_bin(session, work_line_id=3, arrival_id=work.arrival_id, now_ms=100)
            == "WAITING"
        )

        assert await flow.advance(session, task_id="t", now_ms=100) == (
            "CONFIRMING" if status == "SUCCESS" else "WAITING"
        )

    if status == "SUCCESS":
        await no_work(host, {"result": "COMPLETED"})
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=110) == "COMPLETED"


async def test_terminal_cleanup_preserves_original_bin_closure(s5_roller):
    from datetime import UTC, datetime, timedelta

    from app.businesses.smt_auto.models import AutoOutboundDecisionRound
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
        task = await session.get(AutoTask, "t")
        task.first_plan_wait_ms, task.first_plan_deadline_ms = 10, 11

    work = await host.scan()
    await no_work(host)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    host.roller.production.task_flow = flow
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=31)
            == "RELEASING"
        )
        command = (await session.get(AutoBinWork, work.id)).release_command_code
    assert (await host.result(command)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=100)
            == "RELEASED"
        )
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"
    await no_work(host, {"result": "COMPLETED"})

    async with host.factory.begin() as session:
        assert await flow.advance(session, task_id="t", now_ms=110) == "COMPLETED"
        query = {"work_line_id": 1, "bin_code": work.bin_code}
        assert await flow.original_inprocess(session, **query) == ()
        original_messages = (
            await session.scalar(select(AutoOutboundDecisionRound.message_id)),
            await session.scalar(
                select(AutoTaskRequest.message_id).where(AutoTaskRequest.purpose == "COMPLETION")
            ),
        )
        old = datetime.now(UTC) - timedelta(days=100)
        for message_id in original_messages:
            (await session.get(ReliableMessage, message_id)).updated_at = old
        unrelated = ReliableMessage(
            target="decisions",
            operation="unrelated",
            operation_id="unrelated",
            request_raw=b"{}",
            status="DONE",
            attempts=0,
            next_attempt_at=old,
            created_at=old,
            updated_at=old,
        )
        session.add(unrelated)

    await run_cleanup(host.factory, 90, batch=1)

    async with host.factory.begin() as session:
        assert await session.get(ReliableMessage, unrelated.id) is None
        assert await flow.original_inprocess(session, **query) == ()
        for message_id in original_messages:
            assert await session.get(ReliableMessage, message_id) is not None

    await host.scan(timestamp=120)
    async with host.factory() as session:
        arrival = await session.scalar(select(AutoBinWork).where(AutoBinWork.id != work.id))
        assert arrival.bin_code == work.bin_code
        assert arrival.association == "NO_TASK"
        assert arrival.production_hold_reason is None


async def test_release_and_physical_json_never_erase_unresolved_original_scope(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        old = await session.scalar(select(AutoBinWork))
        old.terminal_basis = {"kind": "NO_WORK", "round_id": 9, "operation_id": "fabricated"}
        old.terminal_at_ms = 1
        old.release_consumed_at_ms = 1
        old.release_result_snapshot = {"result": "SUCCESS"}
        flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=old.arrival_id, now_ms=10)
            == "WAITING"
        )
        original = await flow.original_inprocess(session, work_line_id=1, bin_code="BIN")
        assert [row.arrival_id for row in original] == ["arrival"]
        assert await flow.original_inprocess(session, work_line_id=2, bin_code="BIN") == ()


async def test_release_rechecks_public_identity_even_after_saved_success(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
    work = await host.scan()
    await no_work(host)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=31)
        saved = await session.get(AutoBinWork, work.id)
        command = saved.release_command_code
    response = await host.result(command)
    assert response.status_code == 200
    async with host.factory.begin() as session:
        await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=100)
        saved = await session.get(AutoBinWork, work.id)
        saved.device_code = "WRONG"
        assert await flow.original_inprocess(session, work_line_id=1, bin_code=saved.bin_code)


async def test_release_rollback_restores_original_intent_without_second_move(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
    work = await host.scan()
    await no_work(host)
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
    with pytest.raises(RuntimeError):
        async with host.factory.begin() as session:
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=31)
            raise RuntimeError("outer rollback")
    async with host.factory.begin() as session:
        saved = await session.get(AutoBinWork, work.id)
        assert saved.release_command_code is None and saved.release_decided_at_ms is None
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0
        await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=40)
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 1


async def test_plate_physical_json_and_timestamps_cannot_prove_real_closure(s5_roller):
    from app.businesses.smt_auto.models import AutoOutboundPlate
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
    work = await host.scan()
    await no_work(
        host,
        {
            "result": "READY",
            "cell_id": "BIN3",
            "cell_index": 3,
            "bin_type": 3,
            "reel_layer": 1,
            "reel_thickness": 20,
            "reel_diameter": 15,
        },
    )
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoOutboundPlate))
        plate.physical_closure_basis = {"result": "SUCCESS"}
        plate.direct_obligations_closed_at_ms = 31
        plate.pick_consumed_at_ms = 31
        plate.report_consumed_at_ms = 31
        plate.pending = False
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=100)
            == "WAITING"
        )
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("broken", [None, "link", "due", "context", "unresolved"])
async def test_wait_successor_original_chain_controls_release_and_completion(
    s5_roller, direction, broken
):
    from app.businesses.smt_auto.models import AutoInboundDecisionRound, AutoOutboundDecisionRound
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
        task = await session.get(AutoTask, "t")
        task.business_type = direction
        task.first_plan_wait_ms, task.first_plan_deadline_ms = 10, 11
    work = await host.scan()
    await no_work(host, {"result": "WAIT", "retry_after_ms": 1000})
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        first = await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        assert await flow.advance(session, task_id="t", now_ms=100) == "WAITING"
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=100)
            == "WAITING"
        )
        still = await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1029
        )
        assert still.id == first.id
        assert await flow.advance(session, task_id="t", now_ms=1030) == "WAITING"
        successor = await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1030
        )
        assert successor.previous_round_id == first.id and successor.round_no == 2
        assert await flow.advance(session, task_id="t", now_ms=1040) == "WAITING"
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1040)
            == "WAITING"
        )
    await no_work(host)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1040
        )
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1041)
            == "RELEASING"
        )
        command = (await session.get(AutoBinWork, work.id)).release_command_code
    assert (await host.result(command)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=1100)
            == "RELEASED"
        )
        model = AutoInboundDecisionRound if direction == "inbound" else AutoOutboundDecisionRound
        rounds = tuple(await session.scalars(select(model).order_by(model.round_no)))
        assert len(rounds) == 2 and not any(r.unresolved for r in rounds)
        if broken == "link":
            rounds[1].previous_round_id = None
        elif broken == "due":
            rounds[0].next_eligible_at_ms = 2000
        elif broken == "context":
            rounds[0].request_data = dict(rounds[0].request_data, scanned_at=999)
        elif broken == "unresolved":
            rounds[0].unresolved = True
        assert await flow.advance(session, task_id="t", now_ms=1100) == (
            "CONFIRMING" if broken is None else "WAITING"
        )
    if broken is None:
        await no_work(host, {"result": "COMPLETED"})
        async with host.factory.begin() as session:
            assert await flow.advance(session, task_id="t", now_ms=1200) == "COMPLETED"


async def test_readable_no_task_arrival_releases_without_rebinding_to_later_task(s5_roller):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = s5_roller
    flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
    async with host.factory.begin() as session:
        await session.delete(await session.scalar(select(AutoBinWork)))
        task = await session.get(AutoTask, "t")
        task.status = "QUEUED"
    work = await host.scan()
    assert work.association == "NO_TASK" and work.task_id is None
    async with host.factory.begin() as session:
        task = await session.get(AutoTask, "t")
        task.status = "PREPARING"
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20)
            == "RELEASING"
        )
        saved = await session.get(AutoBinWork, work.id)
        command = saved.release_command_code
        assert saved.task_id is None and command
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
    assert (await host.result(command)).status_code == 200
    async with host.factory.begin() as session:
        assert (
            await flow.consume_release(
                session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
            )
            == "RELEASED"
        )
        assert await flow.original_inprocess(session, work_line_id=1, bin_code=work.bin_code) == ()
