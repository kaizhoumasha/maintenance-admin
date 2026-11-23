"""Shared queue update contract through the real SMT WMS callback hosts."""

import asyncio
import json

import pytest
from sqlalchemy import delete, select, update

from app.businesses._components.sorting_task.models import SortingTaskRecord
from app.businesses._components.sorting_task.service import SortingTask
from app.businesses.runtime import build_runtime
from app.businesses.smt_auto.models import AutoTask, AutoTaskInput
from app.businesses.smt_manual.models import ManualInput, ManualTask
from app.core.identifiers import uuid7
from app.integrations import history
from app.integrations.wms_callback import receive
from app.models import WorkLine
from app.reliable import Outbox


@pytest.mark.parametrize(
    ("mode", "task_model", "input_model"),
    [
        ("MANUAL", ManualTask, ManualInput),
        ("AUTO", AutoTask, AutoTaskInput),
    ],
)
async def test_queue_changes_require_a_real_change_and_replay_the_original_receipt(
    callback_db, mode, task_model, input_model
):
    task_id = str(uuid7())
    line_code = f"SMT-{task_id}"
    runtime = build_runtime(
        outbox=Outbox({}, {}, records=history), ecs_commands=object(), session_factory=callback_db
    )

    def message(kind, **data):
        return json.dumps(
            {
                "operation": f"sorting.task.{kind}@v1",
                "operation_id": str(uuid7()),
                "timestamp": 100,
                "data": {"task_id": task_id, **data},
            }
        ).encode()

    async def deliver(raw):
        async with callback_db.begin() as session:
            return await receive(session, raw, runtime=runtime)

    async with callback_db.begin() as session:
        line = WorkLine(
            line_code=line_code, line_name=line_code, business_code=f"smt_{mode.lower()}"
        )
        session.add(line)
        await session.flush()
        line_id = line.id

    try:
        issued = message(
            "issued",
            task_type=mode,
            business_type="outbound",
            workline_code=line_code,
            queue_revision=1,
            dispatch_sequence=100,
            not_before=500,
        )
        assert (await deliver(issued)).status_code == 202
        refused = []
        for fields in (
            {"dispatch_sequence": 100},
            {"not_before": 500},
            {"dispatch_sequence": 100, "not_before": 500},
        ):
            raw = message("queue_changed", queue_revision=2, **fields)
            response = await deliver(raw)
            assert response.status_code == 409
            assert response.body["data"] == {"reason_code": "STATE_CONFLICT"}
            refused.append((raw, response.body))

        gap = await deliver(message("queue_changed", queue_revision=3, dispatch_sequence=200))
        assert gap.status_code == 409 and gap.body["data"] == {"reason_code": "REVISION_CONFLICT"}

        changed = message("queue_changed", queue_revision=2, dispatch_sequence=100, not_before=0)
        accepted = await deliver(changed)
        assert accepted.status_code == 202
        assert (
            await deliver(message("queue_changed", queue_revision=3, dispatch_sequence=200))
        ).status_code == 202
        async with callback_db.begin() as session:
            await session.execute(
                update(task_model).where(task_model.task_id == task_id).values(status="PREPARING")
            )

        for revision in (4, 5):
            frozen = await deliver(
                message("queue_changed", queue_revision=revision, dispatch_sequence=300)
            )
            assert frozen.status_code == 409
            assert frozen.body["data"] == {"reason_code": "STATE_CONFLICT"}
        for raw, original in refused:
            assert (await deliver(raw)).body == original
        replay = await deliver(changed)
        assert replay.status_code == 200 and replay.body["code"] == "DUPLICATE"
        assert replay.body["timestamp"] == accepted.body["timestamp"]
        async with callback_db() as session:
            task = await session.get(task_model, task_id)
            assert (task.queue_revision, task.dispatch_sequence, task.issued_timestamp) == (
                3,
                200,
                100,
            )
            assert task.not_before_ms == 0
    finally:
        async with callback_db.begin() as session:
            await session.execute(delete(input_model).where(input_model.task_id == task_id))
            await session.execute(delete(task_model).where(task_model.task_id == task_id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))


async def test_concurrent_first_issue_accepts_only_one_sorting_task_owner(callback_db, monkeypatch):
    if callback_db.kw["bind"].dialect.name != "postgresql":
        pytest.skip("SQLite serializes writes before callback dispatch")

    task_id = str(uuid7())
    runtime = build_runtime(
        outbox=Outbox({}, {}, records=history), ecs_commands=object(), session_factory=callback_db
    )
    barrier = asyncio.Barrier(2)
    accept = SortingTask.accept

    async def accept_after_both_resolve(component, ctx, payload):
        await asyncio.wait_for(barrier.wait(), timeout=5)
        return await accept(component, ctx, payload)

    monkeypatch.setattr(SortingTask, "accept", accept_after_both_resolve)
    messages = {}
    async with callback_db.begin() as session:
        for mode in ("MANUAL", "AUTO"):
            line_code = f"{mode}-{task_id}"
            session.add(
                WorkLine(
                    line_code=line_code, line_name=line_code, business_code=f"smt_{mode.lower()}"
                )
            )
            messages[mode] = json.dumps(
                {
                    "operation": "sorting.task.issued@v1",
                    "operation_id": str(uuid7()),
                    "timestamp": 100,
                    "data": {
                        "task_id": task_id,
                        "task_type": mode,
                        "business_type": "outbound",
                        "workline_code": line_code,
                        "queue_revision": 1,
                        "dispatch_sequence": 1,
                    },
                }
            ).encode()

    async def deliver(raw):
        async with callback_db.begin() as session:
            return await receive(session, raw, runtime=runtime)

    try:
        replies = dict(
            zip(
                messages,
                await asyncio.gather(*(deliver(r) for r in messages.values())),
                strict=True,
            )
        )
        assert sorted(reply.status_code for reply in replies.values()) == [202, 409]
        async with callback_db() as session:
            row = await session.get(SortingTaskRecord, task_id)
            assert replies[row.task_type].status_code == 202
            for mode, model in (("MANUAL", ManualInput), ("AUTO", AutoTaskInput)):
                assert bool(
                    await session.scalar(select(model.task_id).where(model.task_id == task_id))
                ) == (mode == row.task_type)
        monkeypatch.setattr(SortingTask, "accept", accept)
        for mode, reply in replies.items():
            replay = await deliver(messages[mode])
            expected = (
                {**reply.body, "code": "DUPLICATE"} if reply.status_code == 202 else reply.body
            )
            assert replay.body == expected
    finally:
        async with callback_db.begin() as session:
            for model in (ManualInput, AutoTaskInput, SortingTaskRecord):
                await session.execute(delete(model).where(model.task_id == task_id))
            await session.execute(
                delete(WorkLine).where(
                    WorkLine.line_code.in_([f"{mode}-{task_id}" for mode in messages])
                )
            )
