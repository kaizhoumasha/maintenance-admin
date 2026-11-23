import pytest


async def test_feed_cursor_waits_for_original_success_then_opens_one_window(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    before = (await flow.current_rows("face_work"))[0]
    assert before["phase"] == "FEED_MOVE" and before["feed_cursor"] == 0
    await flow.advance_current("FACE", face_id)
    unchanged = (await flow.current_rows("face_work"))[0]
    assert unchanged["current_transport_task_id"] == before["current_transport_task_id"]
    assert unchanged["step_id"] == before["step_id"]
    await flow.current_bins(2)
    await flow.complete_transport(before["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    current = (await flow.current_rows("face_work"))[0]
    assert current["feed_cursor"] == 4
    assert current["window_kind"] == "SEGMENT"
    assert current["phase"] == "WAIT_RETURN_DECISION"
    assert len(current["return_members"]) == 2


@pytest.mark.parametrize("approved", [1, 2])
async def test_only_ready_prefix_executes_and_tail_keeps_fifo_eligibility(roller_current, approved):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    feed = (await flow.current_rows("face_work"))[0]
    await flow.current_bins(3)
    await flow.complete_transport(feed["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    await flow.decide_current(
        face["return_message_id"],
        dict(
            result="READY",
            moves=[
                dict(
                    sequence_no=i,
                    bin_code=m["bin_code"],
                    target=dict(type="RACK_BIN_SLOT", rack_id="R1", rack_face="A", slot_id=f"R{i}"),
                )
                for i, m in enumerate(face["return_members"][:approved], 1)
            ],
        ),
    )
    await flow.advance_current("FACE", face_id)
    moving = (await flow.current_rows("face_work"))[0]
    rounds = await flow.current_rows("bin_return")
    assert len([r for r in rounds if r["return_state"] == "MOVING"]) == approved
    assert len([r for r in rounds if r["return_state"] == "READY"]) == 3 - approved
    await flow.complete_transport(moving["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["feed_cursor"] == 4 and after["phase"] == "FEED_MOVE"
    assert after["window_id"] is None
    assert (
        len([r for r in await flow.current_rows("bin_return") if r["close_reason"] == "COMPLETED"])
        == approved
    )


async def test_no_batch_does_not_reopen_same_segment_window(roller_current):
    flow = roller_current
    face_id = await flow.ready_supply_current(count=5)
    feed = (await flow.current_rows("face_work"))[0]
    await flow.current_bins(1)
    await flow.complete_transport(feed["current_transport_task_id"])
    await flow.advance_current("FACE", face_id)
    face = (await flow.current_rows("face_work"))[0]
    await flow.decide_current(face["return_message_id"], {"result": "NO_BATCH"})
    await flow.advance_current("FACE", face_id)
    after = (await flow.current_rows("face_work"))[0]
    assert after["phase"] == "FEED_MOVE" and after["window_id"] is None
    await flow.advance_current("FACE", face_id)
    assert (await flow.current_rows("face_work"))[0]["return_message_id"] is None
