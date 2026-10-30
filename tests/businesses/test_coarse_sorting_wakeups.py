"""Committed inputs wake the concrete worker; pending facts recover lost notifications."""

import asyncio

import pytest
from sqlalchemy import func, select


def instrument(flow, monkeypatch):
    from app import business_wakeups as notifications
    from app.businesses.runtime import build_runtime
    from app.core import get_settings
    from app.main import app
    from app.tasks import business_tasks as tasks

    flow.notices, flow.fail_publication = [], False

    def publish(business):
        if flow.fail_publication:
            raise RuntimeError("synthetic broker unavailable")
        flow.notices.append(business)

    monkeypatch.setattr(notifications, "enqueue_business", publish)
    monkeypatch.setattr(
        app.state, "business_event_committed", notifications.event_committed, raising=False
    )
    monkeypatch.setattr(
        app.state, "ecs_result_committed", notifications.ecs_result_committed, raising=False
    )
    flow.store.on_result = notifications.wms_result_committed

    def rebuild():
        flow.runtime = build_runtime(
            outbox=flow.outbox, ecs_commands=flow.commands, session_factory=flow.factory
        )
        flow.service = flow.runtime.services["coarse_sorting"]
        monkeypatch.setattr(app.state, "business_runtime", flow.runtime)

    flow.rebuild = rebuild
    rebuild()
    settings = get_settings().model_copy(
        update={
            "DATABASE_URL": flow.factory.kw["bind"].url.render_as_string(hide_password=False),
            "RELIABLE_TARGETS": {
                "targets": {
                    "coarse-wms": {
                        "base_url": flow.outbox.targets["coarse-wms"].base_url,
                        "protocol": "wms-decisions",
                    }
                }
            },
        }
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)

    async def consume():
        business = flow.notices.pop(0)
        assert business == "coarse_sorting"
        return await tasks.advance_pending(flow.factory, business)

    flow.consume = consume
    return flow


async def reach_move(flow):
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == 1
    assert await flow.dispatch() == 1
    assert await flow.consume() == 1
    assert await flow.dispatch() == 1
    assert flow.notices == []  # A delivery ACK is not an ECS Result.
    return (await flow.rows())[0]


async def test_committed_inputs_advance_without_waiting_for_beat(coarse_forward, monkeypatch):
    flow = instrument(coarse_forward, monkeypatch)
    row = await reach_move(flow)
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.consume() == 1
    assert (await flow.rows())[0]["move_result"]["result"]["data"] == {"actual_qty": 1}
    assert (await flow.post(flow.event())).status_code == 200
    assert (await flow.result(row)).status_code == 200
    assert flow.notices == []
    assert await flow.outbound_counts() == (2, 1)


async def test_scan_rollback_does_not_wake_or_leave_a_receipt(coarse_forward, monkeypatch):
    from app.models import CallbackReceipt

    flow = instrument(coarse_forward, monkeypatch)

    async def fail(_):
        raise ValueError("rollback after accepted scan")

    flow.after_accept = fail
    assert (await flow.post(flow.event())).status_code == 503
    assert await flow.rows() == []
    assert flow.notices == []
    async with flow.factory() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(CallbackReceipt)
                .where(CallbackReceipt.device_code.in_(flow.codes))
            )
            == 0
        )


async def test_scan_publish_failure_keeps_ack_and_restart_recovers(coarse_forward, monkeypatch):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    flow.fail_publication = True
    response = await flow.post(flow.event())
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    original = (await flow.rows())[0]
    assert original["wait_reason"] == "AWAITING_VALIDATION_STAGE"
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    saved = (await flow.rows())[0]
    assert saved["id"] == original["id"] and saved["validation_message_id"] is not None
    assert await flow.outbound_counts() == (1, 0)


async def test_wms_publish_failure_preserves_response_and_fallback(coarse_forward, monkeypatch):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == 1
    original = (await flow.rows())[0]
    flow.fail_publication = True
    assert await flow.dispatch() == 1
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    saved = (await flow.rows())[0]
    assert saved["validation_operation_id"] == original["validation_operation_id"]
    assert saved["command_code"] and saved["binding_id"] == "BIND-ORIGINAL"
    assert len(flow.peer.requests) == 1


async def test_result_publish_failure_keeps_ack_and_fallback(coarse_forward, monkeypatch):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    original = await reach_move(flow)
    flow.fail_publication = True
    response = await flow.result(original)
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    saved = (await flow.rows())[0]
    assert saved["command_code"] == original["command_code"]
    assert saved["wait_reason"] == "AWAITING_ALLOCATION_STAGE"
    assert await flow.outbound_counts() == (2, 1)


async def test_result_rollback_does_not_publish_or_consume_success(coarse_forward, monkeypatch):
    from app.controllers import ecs_result

    flow = instrument(coarse_forward, monkeypatch)
    row = await reach_move(flow)
    receive = ecs_result.receive_ecs_result

    async def rolled_back(*args, **kwargs):
        await receive(*args, **kwargs)
        raise ValueError("rollback after result evidence")

    with monkeypatch.context() as guard:
        guard.setattr(ecs_result, "receive_ecs_result", rolled_back)
        assert (await flow.result(row)).status_code == 503
    assert flow.notices == []
    assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
    assert (await flow.result(row)).status_code == 200
    assert await flow.consume() == 1


async def test_restart_recovers_lost_result_notifications_with_original_ids(
    coarse_forward, monkeypatch
):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == 1
    validation = (await flow.rows())[0]
    assert await flow.dispatch() == 1
    flow.notices.clear()  # Simulate loss between committed result and worker delivery.
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    move = (await flow.rows())[0]
    assert move["validation_message_id"] == validation["validation_message_id"]
    assert await flow.dispatch() == 1
    assert (await flow.result(move)).status_code == 200
    flow.notices.clear()
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    saved = (await flow.rows())[0]
    assert saved["command_code"] == move["command_code"]
    assert saved["wait_reason"] == "AWAITING_ALLOCATION_STAGE"
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 0
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


@pytest.mark.parametrize("concurrent", [False, True])
async def test_repeated_wakeups_keep_one_original_request_and_command(
    coarse_forward, monkeypatch, concurrent
):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    if concurrent and flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL business row-lock and serialization contract")
    assert (await flow.post(flow.event())).status_code == 200

    async def repeat():
        business = flow.notices.pop(0)
        if concurrent:
            await asyncio.gather(*(tasks.advance_pending(flow.factory, business) for _ in range(2)))
        else:
            for _ in range(2):
                await tasks.advance_pending(flow.factory, business)

    await repeat()
    assert await flow.outbound_counts() == (1, 0)
    assert await flow.dispatch() == 1
    await repeat()
    assert await flow.outbound_counts() == (2, 1)
    assert await flow.dispatch() == 1
    assert (await flow.result((await flow.rows())[0])).status_code == 200
    await repeat()
    assert (await flow.rows())[0]["wait_reason"] == "AWAITING_ALLOCATION_STAGE"
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


async def test_ng_decision_wakes_but_does_not_send_a_conveyor_command(coarse_forward, monkeypatch):
    flow = instrument(coarse_forward, monkeypatch)
    flow.peer.decision = {"result": "NG", "reason_code": "MOCK_NG"}
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == 1
    assert await flow.dispatch() == 1
    assert await flow.consume() == 1
    assert await flow.outbound_counts() == (1, 0)


async def test_immediate_celery_entry_uses_real_business_factory(coarse_forward, monkeypatch):
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    business = flow.notices.pop()
    assert await asyncio.to_thread(tasks.advance_business_pending.run, business) == 1
    assert (await flow.rows())[0]["validation_message_id"] is not None


async def test_unrelated_result_does_not_wake_coarse_sorting(coarse_forward, monkeypatch):
    from app import business_wakeups as notifications

    flow = instrument(coarse_forward, monkeypatch)
    notifications.wms_result_committed("other.operation@v1")
    notifications.ecs_result_committed("ecs.dev")
    assert flow.notices == []


@pytest.mark.parametrize("pending_count,notification_count", [(1, 1), (25, 10), (100, 20)])
async def test_business_notification_bursts_keep_one_intent_per_pending_scan(
    coarse_forward, monkeypatch, pending_count, notification_count
):
    import json
    from time import perf_counter

    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    for i in range(pending_count):
        assert (await flow.post(flow.event(timestamp=1793206799000 + i))).status_code == 200
    assert flow.notices == ["coarse_sorting"] * pending_count
    started, visited = perf_counter(), 0
    for _ in range(notification_count):
        visited += await asyncio.to_thread(tasks.advance_business_pending.run, flow.notices[0])
    waiting_seconds = perf_counter() - started
    rows = await flow.rows()
    assert {row["wait_reason"] for row in rows} == {"AWAITING_VALIDATION_RESULT"}
    original_messages = {row["validation_message_id"] for row in rows}
    assert len(original_messages) == pending_count
    assert await flow.outbound_counts() == (pending_count, 0)
    while await flow.dispatch():
        pass
    assert len(flow.peer.requests) == pending_count
    assert (
        await asyncio.to_thread(tasks.advance_business_pending.run, "coarse_sorting")
        == pending_count
    )
    rows = await flow.rows()
    original_commands = {row["command_code"] for row in rows}
    assert len(original_commands) == pending_count
    assert await flow.outbound_counts() == (2 * pending_count, pending_count)
    while await flow.dispatch():
        pass
    assert len(flow.peer.commands) == pending_count
    for row in rows:
        assert (await flow.result(row)).status_code == 200
    assert (
        await asyncio.to_thread(tasks.advance_business_pending.run, "coarse_sorting")
        == pending_count
    )
    assert {row["wait_reason"] for row in await flow.rows()} == {"AWAITING_ALLOCATION_STAGE"}
    for _ in range(notification_count):
        assert await asyncio.to_thread(tasks.advance_business_pending.run, "coarse_sorting") == 0
    assert {row["command_code"] for row in await flow.rows()} == original_commands
    assert {row["validation_message_id"] for row in await flow.rows()} == original_messages
    assert await flow.outbound_counts() == (2 * pending_count, pending_count)
    print(
        "BUSINESS_WAKEUP_METRICS "
        + json.dumps(
            {
                "database": flow.factory.kw["bind"].dialect.name,
                "execution": "celery_task_body_with_owned_engine",
                "pending": pending_count,
                "delivered_notifications": notification_count,
                "waiting_visits": visited,
                "waiting_seconds": round(waiting_seconds, 3),
                "closed_loop_seconds": round(perf_counter() - started, 3),
                "wms_requests": len(flow.peer.requests),
                "ecs_commands": len(flow.peer.commands),
            }
        )
    )


async def test_business_wakeup_reaches_later_pages_beyond_blocked_pending(
    coarse_forward, monkeypatch
):
    from sqlalchemy import update

    from app.models import WorkLine
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    for i in range(101):
        assert (await flow.post(flow.event(timestamp=1793206799000 + i))).status_code == 200
    assert (await flow.post(flow.event(device=1, timestamp=1793206800000))).status_code == 200
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine)
            .where(WorkLine.id == flow.line_ids[0])
            .values(version=WorkLine.version + 1)
        )
    assert await tasks.advance_pending(flow.factory, flow.notices[0]) == 102
    rows = await flow.rows()
    blocked = [row for row in rows if row["device_code"] == flow.codes[0]]
    ready = [row for row in rows if row["device_code"] == flow.codes[1]]
    assert len(blocked) == 101 and {row["wait_reason"] for row in blocked} == {
        "ORIGINAL_BINDING_CHANGED"
    }
    assert len(ready) == 1 and ready[0]["wait_reason"] == "AWAITING_VALIDATION_RESULT"
    assert await flow.outbound_counts() == (1, 0)


async def test_pending_sweep_finishes_before_newly_accepted_work(coarse_forward, monkeypatch):
    from app.businesses.coarse_sorting.service import CoarseSortingService
    from app.tasks import business_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    original = CoarseSortingService.advance
    inserted = False

    async def accept_during_advance(service, scan_id):
        nonlocal inserted
        result = await original(service, scan_id)
        if not inserted:
            inserted = True
            assert (await flow.post(flow.event(timestamp=1793206801000))).status_code == 200
        return result

    monkeypatch.setattr(CoarseSortingService, "advance", accept_during_advance)
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 1
    rows = await flow.rows()
    assert rows[0]["validation_message_id"] is not None
    assert rows[1]["validation_message_id"] is None
    assert await tasks.advance_pending(flow.factory, "coarse_sorting") == 2
    assert await flow.outbound_counts() == (2, 0)
