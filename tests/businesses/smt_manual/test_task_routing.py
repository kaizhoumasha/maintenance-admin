import asyncio
import json

import pytest
from sqlalchemy import select, update

from app.models import CallbackReceipt, ReliableMessage, WorkLine


async def test_queue_rejection_is_stable_but_success_replays_after_claim(manual_flow):
    flow = manual_flow
    await flow.issue()
    no_change = flow.message(
        "queue_changed", dict(task_id="T1", queue_revision=2, dispatch_sequence=100)
    )
    first = await flow.deliver(no_change)
    assert first.status_code == 409 and first.body["data"] == {"reason_code": "STATE_CONFLICT"}
    changed = flow.message(
        "queue_changed", dict(task_id="T1", queue_revision=2, dispatch_sequence=1), timestamp=999
    )
    assert (await flow.deliver(changed)).status_code == 202
    assert (
        await flow.deliver(
            flow.message("queue_changed", dict(task_id="T1", queue_revision=2, dispatch_sequence=1))
        )
    ).status_code == 409
    assert await flow.prepare() == "T1"
    assert (await flow.deliver(no_change)).body == first.body
    assert (await flow.deliver(changed)).body["code"] == "DUPLICATE"
    assert (
        await flow.deliver(
            flow.message("queue_changed", dict(task_id="T1", queue_revision=3, dispatch_sequence=2))
        )
    ).status_code == 409
    assert (await flow.rows("task"))[0]["issued_timestamp"] == 100


async def test_original_task_owner_survives_workline_reconfiguration(manual_flow):
    flow = manual_flow
    await flow.issue()
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(business_code="another")
        )
    raw = flow.message("cancel", dict(task_id="T1", cancel_scope="TASK"))
    assert (await flow.deliver(raw)).status_code == 202
    async with flow.factory() as session:
        owner = await flow.task.read_task_owner(session, task_id="T1")
        assert owner.business_code == "smt_manual" and owner.business_type == "outbound"
        assert not (
            await flow.ports.read_current_task(session, work_line_id=flow.lines[0])
        ).is_valid


async def test_first_issue_rejects_missing_or_mismatched_workline(manual_flow):
    flow = manual_flow
    missing = await flow.issue("MISSING", workline_code="DOES-NOT-EXIST")
    assert missing.status_code == 409
    assert missing.body["data"] == {"reason_code": "REFERENCE_CONFLICT"}
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(business_code="another")
        )
    mismatch = await flow.issue("MISMATCH")
    assert mismatch.status_code == 409
    assert mismatch.body["data"] == {"reason_code": "STATE_CONFLICT"}
    assert await flow.rows("task") == []


@pytest.mark.parametrize("second_line", [1, 2])
async def test_concurrent_first_issue_keeps_one_task_and_stable_conflict(
    manual_flow, monkeypatch, second_line
):
    from app.integrations.wms_callback import CallbackResponse, receiver

    flow = manual_flow
    if second_line == 2:
        original_read = flow.task.repository.task
        absent = asyncio.Barrier(2)

        async def read_together(session, task_id, *, lock=False):
            row = await original_read(session, task_id, lock=lock)
            if lock and row is None:
                async with asyncio.timeout(5):
                    await absent.wait()
            return row

        monkeypatch.setattr(flow.task.repository, "task", read_together)
    originals = [
        flow.message(
            "issued",
            dict(
                task_id="SHARED-ID",
                task_type="MANUAL",
                business_type=direction,
                workline_code=f"SMT-M1-{line}",
                queue_revision=1,
                dispatch_sequence=100,
            ),
            timestamp=timestamp,
        )
        for line, direction, timestamp in (
            (1, "outbound", 100),
            (second_line, "inbound" if second_line == 2 else "outbound", 101),
        )
    ]
    responses = await asyncio.gather(
        *(flow.deliver(raw) for raw in originals), return_exceptions=True
    )
    observed = [
        r.status_code if isinstance(r, CallbackResponse) else type(r).__name__ for r in responses
    ]
    assert sorted(observed, key=str) == [202, 409]
    winner, loser = (observed.index(code) for code in (202, 409))
    conflict = responses[loser]
    assert conflict.body["data"] == {"reason_code": "STATE_CONFLICT"}
    original = json.loads(originals[winner])
    tasks = await flow.rows("task")
    assert len(tasks) == 1
    assert tasks[0]["issued_operation_id"] == original["operation_id"]
    assert tasks[0]["issued_timestamp"] == original["timestamp"]
    assert tasks[0]["issued_data"] == original["data"]
    assert len(await flow.rows("input")) == 1
    assert flow.notices == ["smt_manual"]
    loser_id = json.loads(originals[loser])["operation_id"]
    async with flow.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(CallbackReceipt.operation_id == loser_id)
        )
        assert receipt.status_code == 409 and receipt.body == conflict.body
        created_at = receipt.created_at
    monkeypatch.setattr(receiver, "unix_ms", lambda _: 2222)
    assert (await flow.deliver(originals[loser])).body == conflict.body
    assert (await flow.deliver(originals[winner])).body["code"] == "DUPLICATE"
    winner_line = 1 if winner == 0 else second_line
    assert await flow.prepare(winner_line) == "SHARED-ID"
    assert (await flow.deliver(originals[loser])).body == conflict.body
    assert (await flow.deliver(originals[winner])).body["code"] == "DUPLICATE"
    assert await flow.prepare(winner_line) is None
    async with flow.factory() as session:
        receipt = await session.scalar(
            select(CallbackReceipt).where(CallbackReceipt.operation_id == loser_id)
        )
        assert receipt.created_at == created_at and receipt.body == conflict.body
        messages = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.business_key == "business:smt_manual")
            )
        )
        assert len(messages) == 1
        assert json.loads(messages[0].request_raw)["data"] == {
            "task_id": "SHARED-ID",
            "workline_code": original["data"]["workline_code"],
        }
