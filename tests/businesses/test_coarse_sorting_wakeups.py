"""Committed inputs wake the concrete worker; pending facts recover lost notifications."""

import asyncio

import pytest
from sqlalchemy import func, select


def instrument(flow, monkeypatch):
    from app import business_wakeups as notifications
    from app.businesses.runtime import build_runtime
    from app.core import get_settings
    from app.main import app
    from app.tasks import coarse_sorting_tasks as tasks

    flow.notices, flow.failed_sources = [], set()

    def publish(business, source, identity):
        if source in flow.failed_sources:
            raise RuntimeError("synthetic broker unavailable")
        flow.notices.append((business, source, identity))

    monkeypatch.setattr(notifications, "enqueue_advance", publish)
    monkeypatch.setattr(
        app.state, "ecs_result_committed", notifications.ecs_result_committed, raising=False
    )
    flow.store.on_result = notifications.wms_result_committed

    def rebuild():
        flow.runtime = build_runtime(
            outbox=flow.outbox,
            ecs_commands=flow.commands,
            session_factory=flow.factory,
            enqueue_advance=publish,
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
        business, source, identity = flow.notices.pop(0)
        assert business == "coarse_sorting"
        return await tasks.advance_one(flow.factory, source, identity)

    flow.consume = consume
    return flow


async def reach_move(flow):
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    assert await flow.consume() == "AWAITING_MOVE_RESULT"
    assert await flow.dispatch() == 1
    assert flow.notices == []  # A delivery ACK is not an ECS Result.
    return (await flow.rows())[0]


async def test_committed_inputs_advance_without_waiting_for_beat(coarse_forward, monkeypatch):
    flow = instrument(coarse_forward, monkeypatch)
    row = await reach_move(flow)
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.consume() == "AWAITING_ALLOCATION_STAGE"
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
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    flow.failed_sources.add("scan")
    response = await flow.post(flow.event())
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    original = (await flow.rows())[0]
    assert original["wait_reason"] == "AWAITING_VALIDATION_STAGE"
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory) == 1
    saved = (await flow.rows())[0]
    assert saved["id"] == original["id"] and saved["validation_message_id"] is not None
    assert await flow.outbound_counts() == (1, 0)


async def test_wms_publish_failure_preserves_response_and_fallback(coarse_forward, monkeypatch):
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == "AWAITING_VALIDATION_RESULT"
    original = (await flow.rows())[0]
    flow.failed_sources.add("wms")
    assert await flow.dispatch() == 1
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory) == 1
    saved = (await flow.rows())[0]
    assert saved["validation_operation_id"] == original["validation_operation_id"]
    assert saved["command_code"] and saved["binding_id"] == "BIND-ORIGINAL"
    assert len(flow.peer.requests) == 1


async def test_result_publish_failure_keeps_ack_and_fallback(coarse_forward, monkeypatch):
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    original = await reach_move(flow)
    flow.failed_sources.add("ecs")
    response = await flow.result(original)
    assert response.status_code == 200 and response.json() == {"code": 200, "message": "ACK"}
    assert flow.notices == []
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory) == 1
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
    assert await flow.consume() == "AWAITING_ALLOCATION_STAGE"


async def test_restart_recovers_lost_result_notifications_with_original_ids(
    coarse_forward, monkeypatch
):
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    assert await flow.consume() == "AWAITING_VALIDATION_RESULT"
    validation = (await flow.rows())[0]
    assert await flow.dispatch() == 1
    flow.notices.clear()  # Simulate loss between committed result and worker delivery.
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory) == 1
    move = (await flow.rows())[0]
    assert move["validation_message_id"] == validation["validation_message_id"]
    assert await flow.dispatch() == 1
    assert (await flow.result(move)).status_code == 200
    flow.notices.clear()
    flow.rebuild()
    assert await tasks.advance_pending(flow.factory) == 1
    saved = (await flow.rows())[0]
    assert saved["command_code"] == move["command_code"]
    assert saved["wait_reason"] == "AWAITING_ALLOCATION_STAGE"
    assert await tasks.advance_pending(flow.factory) == 0
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


@pytest.mark.parametrize("concurrent", [False, True])
async def test_repeated_wakeups_keep_one_original_request_and_command(
    coarse_forward, monkeypatch, concurrent
):
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    if concurrent and flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL business row-lock and serialization contract")
    assert (await flow.post(flow.event())).status_code == 200

    async def repeat():
        _, source, identity = flow.notices.pop(0)
        if concurrent:
            await asyncio.gather(
                *(tasks.advance_one(flow.factory, source, identity) for _ in range(2))
            )
        else:
            for _ in range(2):
                await tasks.advance_one(flow.factory, source, identity)

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
    assert await flow.consume() == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    assert await flow.consume() == "AWAITING_NG_STAGE"
    assert await flow.outbound_counts() == (1, 0)


async def test_immediate_celery_entry_uses_real_business_factory(coarse_forward, monkeypatch):
    from app.tasks import coarse_sorting_tasks as tasks

    flow = instrument(coarse_forward, monkeypatch)
    assert (await flow.post(flow.event())).status_code == 200
    _, source, identity = flow.notices.pop()
    assert await asyncio.to_thread(tasks.advance_coarse_sorting_one.run, source, identity) == (
        "AWAITING_VALIDATION_RESULT"
    )
    assert (await flow.rows())[0]["validation_message_id"] is not None


async def test_unrelated_result_does_not_wake_coarse_sorting(coarse_forward, monkeypatch):
    from app import business_wakeups as notifications

    flow = instrument(coarse_forward, monkeypatch)
    notifications.wms_result_committed(123, "other.operation@v1", "original")
    notifications.ecs_result_committed("ecs.dev", "original-command")
    assert flow.notices == []
