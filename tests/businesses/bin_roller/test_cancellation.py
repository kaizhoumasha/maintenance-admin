"""Member withdrawal preserves already started obligations while its TASK remains valid."""

import pytest

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange


async def withdraw(flow, face_id):
    async with flow.factory.begin() as session:
        face = await flow.roller.original_record(session, "face_work", face_id)
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1,
                face["scope_id"],
                "withdraw",
                (),
                (SourceFaceRef(face["plan_revision"], face["rack_id"], face["rack_face"]),),
            ),
        )


async def test_cancel_unstarted_demand_stops_new_allocation(roller_current):
    flow = roller_current
    face_id = await flow.demand_current()
    await withdraw(flow, face_id)
    await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    assert face["close_reason"] == "WITHDRAWN" and face["feed_operation_id"] is None
    assert face["rack_job_id"] is None
    async with flow.factory() as session:
        progress = (
            await flow.roller.get_supply_progress(session, work_line_id=1, task_id="task-original")
        )[0]
        assert not progress.pending and not progress.feed_complete


@pytest.mark.parametrize("before_result", [True, False])
async def test_cancelled_in_progress_rack_requests_wms_on_arrival(roller_current, before_result):
    flow = roller_current
    face_id = await flow.demand_current()
    await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=face["rack_job_id"])
    if before_result:
        await withdraw(flow, face_id)
    await flow.complete_transport(job.actions[0].transport_task_id)
    await flow.advance_current("FACE", face_id)
    if not before_result:
        await withdraw(flow, face_id)
    face = (await flow.current_rows("face_work"))[0]
    assert face["closed_at"] is None and face["demand_state"] == "WITHDRAWN"
    assert face["feed_message_id"] and face["preparation_action_id"] == job.actions[0].id


async def test_cancel_after_ready_keeps_remaining_segments(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    first = (await flow.current_rows("face_work"))[0]
    await withdraw(flow, face_id)
    await flow.complete_transport(first["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["feed_cursor"] == 4 and after["phase"] == "FEED_MOVE"
    assert after["current_transport_task_id"] != first["current_transport_task_id"]
    assert after["feed_message_id"] == first["feed_message_id"]


async def test_late_ready_belongs_to_original_segment(roller_current):
    flow = roller_current
    old_id = await flow.ready_supply_current(count=None)
    old = (await flow.current_rows("face_work"))[0]
    await withdraw(flow, old_id)
    new_id = await flow.demand_current(revision=2)
    await flow.decide_current(
        old["feed_message_id"],
        dict(
            result="READY",
            bins=[
                dict(
                    bin_code="A000000001",
                    source_locator=dict(
                        type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id="S1"
                    ),
                )
            ],
        ),
    )
    await flow.advance_current("FACE", old_id)
    await flow.advance_current("FACE", new_id)
    rows = {r["id"]: r for r in await flow.current_rows("face_work")}
    assert (
        rows[old_id]["phase"] == "FEED_MOVE"
        and rows[old_id]["feed_message_id"] == old["feed_message_id"]
    )
    assert rows[new_id]["feed_message_id"] is None
