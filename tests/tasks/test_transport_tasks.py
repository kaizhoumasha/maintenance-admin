import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.identifiers import uuid7
from app.models import ReliableMessage, TransportTask
from app.tasks.transport_tasks import scan_stuck

NOW = datetime.now(UTC)
SUBMIT = "transport.task.submit@v1"


def task(age_minutes, **fields):
    task_id = "transport-" + str(uuid7())
    return TransportTask(
        client_request_id=str(uuid7()),
        transport_task_id=task_id,
        operation_id=str(uuid7()),
        kind="RACK_MOVE",
        input_digest="a" * 64,
        workline_id="line",
        created_at=NOW - timedelta(minutes=age_minutes),
        **fields,
    )


def submit_message(task_id, status):
    return ReliableMessage(
        target="wms",
        operation=SUBMIT,
        operation_id=str(uuid7()),
        request_raw=b"{}",
        status=status,
        attempts=0,
        next_attempt_at=NOW,
        business_key=task_id,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_only_old_tasks_without_a_determined_outcome_raise_an_alert(db, caplog):
    fresh = task(30)
    waiting = task(180, cycle_id="cycle-1")
    expired = task(180)
    unknown = task(180, outcome_status="UNKNOWN")
    parked = task(180, pending_outcome={"revision": 1, "data": {}})
    done = task(180, outcome_status="SUCCEEDED")
    cancelled = task(180, outcome_status="CANCELLED")
    db.add_all([fresh, waiting, expired, unknown, parked, done, cancelled])
    db.add(submit_message(expired.transport_task_id, "EXPIRED"))
    await db.commit()
    caplog.set_level(logging.WARNING)
    assert await scan_stuck(async_sessionmaker(db.bind), 120, now=NOW) == 4
    reasons = {r.transport_task_id: r.reason for r in caplog.records if hasattr(r, "reason")}
    assert reasons == {
        waiting.transport_task_id: "RESULT_MISSING",
        expired.transport_task_id: "SUBMIT_NOT_DELIVERED",
        unknown.transport_task_id: "OUTCOME_UNKNOWN",
        parked.transport_task_id: "RESULT_WAITING_FOR_POSITIONS",
    }
    record = next(r for r in caplog.records if r.transport_task_id == waiting.transport_task_id)
    assert record.cycle_id == "cycle-1" and record.waiting_minutes == 180


@pytest.mark.parametrize("minutes", [0, -1, True])
async def test_alert_threshold_must_be_a_positive_integer(db, minutes):
    with pytest.raises((TypeError, ValueError)):
        await scan_stuck(async_sessionmaker(db.bind), minutes)


async def test_every_stuck_task_is_alerted_even_when_older_tasks_remain(db, caplog):
    tasks = [task(180) for _ in range(201)]
    db.add_all(tasks)
    await db.commit()
    caplog.set_level(logging.WARNING)
    expected = {item.transport_task_id for item in tasks}
    for current in (NOW, NOW + timedelta(minutes=10)):
        caplog.clear()
        assert await scan_stuck(async_sessionmaker(db.bind), 120, now=current) == len(tasks)
        alerted = [record.transport_task_id for record in caplog.records]
        assert len(alerted) == len(expected) and set(alerted) == expected
