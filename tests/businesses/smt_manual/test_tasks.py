import asyncio
import json

import pytest
from sqlalchemy import select, update

from app.models import ReliableMessage, WorkLine
from tests.helpers import read_history_body


async def test_manual_directions_share_one_execution_slot(manual_flow):
    flow = manual_flow
    for task_id, direction, timestamp in (
        ("C", "inbound", 101),
        ("B", "outbound", 100),
        ("A", "inbound", 100),
    ):
        raw = flow.message(
            "issued",
            dict(
                task_id=task_id,
                task_type="MANUAL",
                business_type=direction,
                workline_code="SMT-M1-1",
                queue_revision=1,
                dispatch_sequence=10,
            ),
            timestamp=timestamp,
        )
        assert (await flow.deliver(raw)).status_code == 202
    assert await flow.prepare() == "A"
    assert await flow.prepare() is None
    assert (await flow.issue("OTHER", line=2)).status_code == 202
    assert await flow.prepare(2) == "OTHER"
    tasks = await flow.rows("task")
    assert [(r["task_id"], r["status"]) for r in tasks if r["status"] == "PREPARING"] == [
        ("A", "PREPARING"),
        ("OTHER", "PREPARING"),
    ]
    async with flow.factory() as session:
        messages = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.business_key == "business:smt_manual")
            )
        )
        assert len(messages) == 2
        original = await read_history_body(session, messages[0].request_record_id)
    assert json.loads(original)["data"] == {"task_id": "A", "workline_code": "SMT-M1-1"}


async def test_task_identity_and_direction_never_change(manual_flow):
    flow = manual_flow
    assert (await flow.issue()).status_code == 202
    for changes in (
        {"business_type": "inbound"},
        {"task_type": "AUTO"},
        {"workline_code": "SMT-M1-2"},
        {},
    ):
        data = dict(
            task_id="T1",
            task_type="MANUAL",
            business_type="outbound",
            workline_code="SMT-M1-1",
            queue_revision=1,
            dispatch_sequence=100,
        )
        data.update(changes)
        response = await flow.deliver(flow.message("issued", data))
        assert response.status_code == (
            422 if {"business_type", "task_type"} & changes.keys() else 409
        )
    for direction in (None, "wrong"):
        data = dict(
            task_id="INVALID",
            task_type="MANUAL",
            workline_code="SMT-M1-1",
            queue_revision=1,
            dispatch_sequence=1,
        )
        if direction:
            data["business_type"] = direction
        assert (await flow.deliver(flow.message("issued", data))).status_code == 422
    assert (await flow.issue("AUTO", task_type="AUTO")).status_code == 409
    assert len(await flow.rows("task")) == 1


async def test_not_before_and_successor_read_original_queue(manual_flow):
    flow = manual_flow
    assert (await flow.issue("LATER", not_before=2**63 - 2)).status_code == 202
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "UNKNOWN"
        assert not (
            await flow.ports.read_current_task(session, work_line_id=flow.lines[0])
        ).is_valid
    assert await flow.prepare(now_ms=1000) is None
    assert await flow.prepare(now_ms=2**63 - 2) == "LATER"
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "CHECKING"
    assert await flow.accept_prepare("LATER") == "PREPARING"
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "AVAILABLE"
    assert (await flow.rows("task"))[0]["issued_timestamp"] == 100


async def test_prepare_and_task_claim_roll_back_together(manual_flow):
    flow = manual_flow
    await flow.issue()
    assert await flow.prepare(rollback=True) == "T1"
    row = (await flow.rows("task"))[0]
    assert row["status"] == "QUEUED" and row["prepare_message_id"] is None
    async with flow.factory() as session:
        assert not await session.scalar(
            select(ReliableMessage.id).where(ReliableMessage.business_key == "business:smt_manual")
        )
    assert await flow.prepare() == "T1"


@pytest.mark.parametrize("first", ["queue", "claim"])
async def test_queue_update_and_claim_follow_commit_order(manual_flow, wait_for_lock, first):
    from app.integrations.wms_callback import receive

    flow = manual_flow
    await flow.issue("A", dispatch_sequence=100)
    await flow.issue("B", dispatch_sequence=200)
    raw = flow.message("queue_changed", dict(task_id="A", queue_revision=2, dispatch_sequence=300))
    async with flow.factory() as owner:
        if first == "queue":
            response = await receive(owner, raw, runtime=flow.runtime)
            assert response.status_code == 202
            pending = asyncio.create_task(flow.prepare())
        else:
            assert (
                await flow.task.prepare_next(owner, work_line_id=flow.lines[0], now_ms=1000) == "A"
            )
            pending = asyncio.create_task(flow.deliver(raw))
        try:
            await wait_for_lock(pending)
            assert not pending.done()
            await owner.commit()
            result = await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    if first == "queue":
        assert result == "B"
    else:
        assert result.status_code == 409
        assert result.body["data"] == {"reason_code": "STATE_CONFLICT"}


async def test_concurrent_claims_preserve_one_original_prepare(manual_flow):
    flow = manual_flow
    await flow.issue("A")
    await flow.issue("B", direction="inbound")
    assert sorted(await asyncio.gather(flow.prepare(), flow.prepare()), key=str) == ["A", None]
    rows = await flow.rows("task")
    assert len([r for r in rows if r["prepare_operation_id"]]) == 1


async def test_successor_read_failure_does_not_claim_no_successor(manual_flow, monkeypatch):
    async with manual_flow.factory() as session:

        async def unavailable(*args, **kwargs):
            raise RuntimeError("read failed")

        monkeypatch.setattr(session, "get", unavailable)
        with pytest.raises(RuntimeError, match="read failed"):
            await manual_flow.ports.read_successor(session, work_line_id=manual_flow.lines[0])


async def test_eligible_queue_reads_do_not_claim_or_register_prepare(manual_flow):
    flow = manual_flow
    await flow.issue()
    async with flow.factory() as session:
        current = await flow.ports.read_current_task(session, work_line_id=flow.lines[0])
        successor = await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        assert current.task_id is None and not current.is_valid
        assert successor.state == "CHECKING" and successor.evidence["queued_task_ids"] == ["T1"]
        assert not await session.scalar(
            select(ReliableMessage.id).where(ReliableMessage.business_key == "business:smt_manual")
        )
    task = (await flow.rows("task"))[0]
    assert task["status"] == "QUEUED" and task["prepare_operation_id"] is None


async def test_disabled_queue_waits_without_creating_or_replacing_prepare(manual_flow):
    flow = manual_flow
    await flow.issue()
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    assert await flow.prepare() is None
    row = (await flow.rows("task"))[0]
    assert row["status"] == "QUEUED" and row["prepare_message_id"] is None
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=True)
        )
    assert await flow.prepare() == "T1"
    original = (await flow.rows("task"))[0]
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    assert await flow.prepare() is None
    assert (await flow.rows("task"))[0] == original
