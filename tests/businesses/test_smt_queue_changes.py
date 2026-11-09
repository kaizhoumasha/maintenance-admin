"""Shared queue update contract through the real SMT WMS callback hosts."""

import json

import pytest
from sqlalchemy import delete, update

from app.businesses.runtime import build_runtime
from app.businesses.smt_auto.models import AutoTask, AutoTaskInput
from app.businesses.smt_manual.models import ManualInput, ManualTask
from app.core.identifiers import uuid7
from app.integrations import history
from app.integrations.wms_callback import receive
from app.models import WorkLine
from app.reliable import Outbox


@pytest.mark.parametrize(
    ("mode", "task_model", "input_model", "not_before_field"),
    [
        ("MANUAL", ManualTask, ManualInput, "not_before"),
        ("AUTO", AutoTask, AutoTaskInput, "not_before_ms"),
    ],
)
async def test_queue_changes_require_a_real_change_and_replay_the_original_receipt(
    callback_db, mode, task_model, input_model, not_before_field
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
            assert getattr(task, not_before_field) == 0
    finally:
        async with callback_db.begin() as session:
            await session.execute(delete(input_model).where(input_model.task_id == task_id))
            await session.execute(delete(task_model).where(task_model.task_id == task_id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))
