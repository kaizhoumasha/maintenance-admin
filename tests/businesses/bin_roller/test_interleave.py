async def test_five_bin_ordinary_graph_four_plus_one_and_three_returns(roller_lifecycle):
    flow = roller_lifecycle
    await flow.bins(6)
    demand, cycle = await flow.ready_supply()
    first = (await flow.all_rows("feed_segment"))[0]
    assert len(first["members"]) == 4
    await flow.feed_success(first)
    await flow.advance("FEED", demand)
    assert len(await flow.all_rows("return_batch")) == 1
    await flow.complete_return(count=2)
    segments = await flow.all_rows("feed_segment")
    assert [len(s["members"]) for s in segments] == [4, 1]
    assert len(await flow.all_rows("return_batch")) == 1
    await flow.feed_success(segments[-1])
    await flow.advance("FEED", demand)
    await flow.complete_return(count=2)
    await flow.complete_return(count=1)
    await flow.complete_return(no_batch=True)
    assert len(await flow.all_rows("return_batch")) == 4
    assert len(await flow.all_rows("feed")) == 1
    await flow.finish_departure(cycle)
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01", "CTU03"]
    assert [b["status"] for b in await flow.all_rows("return_batch")] == ["SUCCEEDED"] * 3 + [
        "NO_BATCH"
    ]
    assert sum(r["status"] != "COMPLETED" for r in await flow.all_rows("return_round")) == 1


async def test_no_candidate_continues_feed_without_waiting_for_scan4(roller_lifecycle):
    flow = roller_lifecycle
    demand, cycle = await flow.ready_supply()
    segment = (await flow.all_rows("feed_segment"))[0]
    await flow.feed_success(segment)
    await flow.advance("FEED", demand)
    assert [len(s["members"]) for s in await flow.all_rows("feed_segment")] == [4, 1]
    assert not await flow.all_rows("return_batch")
    assert (await flow.all_rows("return_opportunity"))[0]["end_reason"] == "NO_CANDIDATES"
