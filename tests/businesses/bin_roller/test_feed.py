"""Feed contracts through original allocations, current cursor and frozen public moves."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.models import TransportTask


async def test_five_bins_split_four_and_one(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    first = (await flow.current_rows("face_work"))[0]
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=1, task_id="task-original"
        )
        assert progress[0].fed_bin_count == 0
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == first["current_transport_task_id"]
            )
        )
        assert len(task.frozen["moves"]) == 4
        assert task.frozen["moves"][0]["target"] == {
            "kind": "HANDOFF_POSITION",
            "location_code": "IN",
        }
    await flow.complete_transport(first["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    await flow.advance_current("FACE", face_id)
    current = (await flow.current_rows("face_work"))[0]
    assert current["feed_cursor"] == 4 and current["phase"] == "FEED_MOVE"
    async with flow.factory() as session:
        task = await session.scalar(
            select(TransportTask).where(
                TransportTask.transport_task_id == current["current_transport_task_id"]
            )
        )
        assert (
            len(task.frozen["moves"]) == 1
            and task.client_request_id != first["current_client_request_id"]
        )
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=1, task_id="task-original"
        )
        assert progress[0].fed_bin_count == 4
    await flow.complete_transport(current["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=1, task_id="task-original"
        )
        assert progress[0].fed_bin_count == 5


async def test_segment_success_does_not_wait_for_scans(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    await flow.complete_transport(before["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["feed_cursor"] == 4
    assert await flow.current_rows("bin_passage") == await flow.current_rows("bin_return") == []
    assert after["step_id"] != before["step_id"]


async def test_face_done_opens_return_without_fake_feed(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=None)
    before = (await flow.current_rows("face_work"))[0]
    await flow.decide_current(before["feed_message_id"], {"result": "RACK_FACE_DONE"})
    await flow.advance_current("FACE", face_id)
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["close_reason"] == "COMPLETED" and after["feed_cursor"] == 0
    async with flow.factory() as session:
        assert not list(
            await session.scalars(select(TransportTask.id).where(TransportTask.kind == "BIN_MOVE"))
        )
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=1, task_id="task-original"
        )
        assert progress[0].feed_complete and not progress[0].pending
        assert progress[0].fed_bin_count == 0


@pytest.mark.parametrize(
    "data",
    [
        {"result": "READY", "bins": []},
        {"result": "RACK_FACE_DONE", "bins": []},
        {"result": "NO_BATCH"},
        {
            "result": "READY",
            "bins": [
                {
                    "bin_code": "A000000001",
                    "source_locator": {
                        "type": "RACK_BIN_SLOT",
                        "rack_id": "OTHER",
                        "rack_face": "A",
                        "slot_id": "S1",
                    },
                }
            ],
        },
    ],
)
async def test_invalid_manifest_keeps_original_request_pending(roller_current, data):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=None)
    before = (await flow.current_rows("face_work"))[0]
    await flow.decide_current(before["feed_message_id"], data)
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["phase"] == "WAIT_FEED_DECISION" and after["step_id"] is None
    assert after["feed_operation_id"] == before["feed_operation_id"] and after["feed_cursor"] == 0


async def test_recovery_keeps_frozen_segments_and_original_message(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    from app.businesses._components.bin_roller.service import BinRoller

    original = flow.roller
    flow.roller = BinRoller(
        original.context,
        original.capabilities,
        original.production,
        rack_dispatch=original.rack_dispatch,
        tables=original.tables,
    )
    for _ in range(2):
        await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    for key in (
        "step_id",
        "feed_operation_id",
        "feed_message_id",
        "current_client_request_id",
        "current_transport_task_id",
    ):
        assert after[key] == before[key]


async def test_feed_consumption_and_next_segment_roll_back_together(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    await flow.complete_transport(before["current_transport_task_id"])
    with pytest.raises(RuntimeError, match="rollback"):
        async with flow.factory.begin() as session:
            await flow.roller.advance(session, RollerWorkRef("FACE", face_id))
            raise RuntimeError("rollback")
    after = (await flow.current_rows("face_work"))[0]
    assert after["feed_cursor"] == 0 and after["step_id"] == before["step_id"]
    await flow.advance_current("FACE", face_id)
    assert (await flow.current_rows("face_work"))[0]["feed_cursor"] == 4


@pytest.mark.parametrize(
    "mismatch", ["owner", "task", "client", "caller", "cycle", "members", "target", "pending"]
)
async def test_feed_success_requires_original_applied_members_and_target(
    roller_current, monkeypatch, mismatch
):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    await flow.complete_transport(before["current_transport_task_id"])
    port = flow.roller.capabilities.transport
    original_get = port.get_result
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=before["rack_job_id"])
        result = await port.get_result(
            session,
            transport_task_id=before["current_transport_task_id"],
            client_request_id=before["current_client_request_id"],
            caller=job.origin.caller,
        )
    changes = {
        "owner": {"owner_domain": "other"},
        "task": {"transport_task_id": "other"},
        "client": {"client_request_id": "other"},
        "caller": {"workline_id": "other"},
        "cycle": {"cycle_id": "other"},
        "pending": {"has_pending_outcome": True},
    }
    if mismatch in changes:
        bad = replace(result, **changes[mismatch])
    else:
        frozen = deepcopy(result.frozen)
        if mismatch == "members":
            frozen["moves"].pop()
        else:
            frozen["moves"][0]["target"]["location_code"] = "OTHER"
        bad = replace(result, frozen=frozen)

    async def wrong(*args, **kwargs):
        return bad

    monkeypatch.setattr(port, "get_result", wrong)
    await flow.advance_current("FACE", face_id)
    assert (await flow.current_rows("face_work"))[0]["feed_cursor"] == 0
    monkeypatch.setattr(port, "get_result", original_get)
    await flow.advance_current("FACE", face_id)
    assert (await flow.current_rows("face_work"))[0]["feed_cursor"] == 4


async def test_unknown_feed_keeps_original_unresolved_obligation(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    await flow.complete_transport(before["current_transport_task_id"], status="UNKNOWN")
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert (
        after["feed_cursor"] == 0
        and after["current_transport_task_id"] == before["current_transport_task_id"]
    )
    await flow.complete_transport(before["current_transport_task_id"], revision=2)
    await flow.advance_current("FACE", face_id)
    assert (await flow.current_rows("face_work"))[0]["feed_cursor"] == 4


@pytest.mark.asyncio
async def test_inbound_request_is_strict_three_fields_and_opaque_face(roller_feed):
    from pydantic import ValidationError

    from app.businesses._components.bin_roller.request.bin_inbound_batch_v1 import (
        InboundBatch,
    )

    assert InboundBatch(task_id="T1", rack_id="R1", rack_face=" 90 ").model_dump() == {
        "task_id": "T1",
        "rack_id": "R1",
        "rack_face": " 90 ",
    }
    for values in (
        dict(task_id="T1", rack_id="R1", rack_face="A", plan_revision=1),
        dict(task_id="", rack_id="R1", rack_face="A"),
        dict(task_id="T1", rack_id="R1", rack_face=""),
        dict(task_id="T1", rack_id="R1", rack_face="A" * 11),
        dict(task_id="T1", rack_id="R1", rack_face=1),
    ):
        with pytest.raises(ValidationError):
            InboundBatch(**values)
