async def test_five_bin_ordinary_graph_four_plus_one_and_three_returns(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(6)
    demand, cycle = await flow.ready_supply()
    first = (await flow.all_rows("feed_segment"))[0]
    assert len(first["members"]) == 4
    await flow.feed_success(first)
    await flow.advance("FACE", demand)
    assert len(await flow.all_rows("return_batch")) == 1
    await flow.complete_return(count=2)
    segments = await flow.all_rows("feed_segment")
    assert [len(s["members"]) for s in segments] == [1]
    assert not await flow.all_rows("return_batch")
    await flow.feed_success(segments[-1])
    await flow.advance("FACE", demand)
    await flow.complete_return(count=2)
    await flow.complete_return(count=1)
    await flow.complete_return(no_batch=True)
    assert not await flow.all_rows("return_batch")
    assert len(await flow.all_rows("feed")) == 1
    await flow.finish_departure(cycle)
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]
    assert (await flow.all_rows("face_work"))[0]["close_reason"] == "COMPLETED"
    assert sum(r["closed_at"] is None for r in await flow.all_rows("bin_return")) == 1


async def test_no_candidate_continues_feed_without_waiting_for_scan4(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply()
    segment = (await flow.all_rows("feed_segment"))[0]
    await flow.feed_success(segment)
    await flow.advance("FACE", demand)
    await flow.advance("FACE", demand)
    assert [len(s["members"]) for s in await flow.all_rows("feed_segment")] == [1]
    assert not await flow.all_rows("return_batch")
    face = (await flow.all_rows("face_work"))[0]
    assert face["feed_cursor"] == 4 and face["window_id"] is None
