"""Physical FIFO heads are checked before eligibility filtering."""

import pytest


@pytest.mark.parametrize("head", ["unready", "orphan", "unknown"])
async def test_pending_head_is_not_filtered_out(roller_current, head):
    flow = roller_current
    if head == "orphan":
        await flow.scan("SCAN4", "A000000001-B", timestamp=1)
    else:
        await flow.scan("SCAN3", "A000000001-B", timestamp=1)
        await flow.scan("SCAN4", "A000000001-B", timestamp=2)
        if head == "unknown":
            row = (await flow.current_rows("bin_return"))[0]
            await flow.result(row["scan4_command_code"], "SCAN4", status="FAILED")
    await flow.current_bins(1, offset=1)
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)


async def test_nonempty_prefix_does_not_wait_for_four_bins(roller_current):
    flow = roller_current
    await flow.current_bins(1)
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN4", "A000000002-B", timestamp=21)
    await flow.current_bins(1, offset=2)
    async with flow.factory() as session:
        assert [
            r["bin_code"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000001"]


async def test_unknown_same_point_does_not_resend(roller_current):
    flow = roller_current
    await flow.current_bins(1)
    before = (await flow.current_rows("bin_return"))[0]
    await flow.scan("SCAN4", "NOREAD", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=31)
    row = next(
        r for r in await flow.current_rows("bin_return") if r["bin_code"] == before["bin_code"]
    )
    assert row["scan4_command_code"] == before["scan4_command_code"]
    assert row["first_scan4_event_id"] == before["first_scan4_event_id"]


async def test_first_scan4_arrival_orders_candidates_even_when_scan3_order_differs(roller_current):
    flow = roller_current
    for n in (1, 2):
        await flow.scan("SCAN3", f"A{n:09}-B", timestamp=n)
    for n in (2, 1):
        await flow.scan("SCAN4", f"A{n:09}-B", timestamp=100 - n)
        row = next(r for r in await flow.current_rows("bin_return") if r["bin_code"] == f"A{n:09}")
        await flow.result(row["scan4_command_code"], "SCAN4")
        await flow.advance_current("RETURN", row["id"])
    async with flow.factory() as session:
        assert [
            r["bin_code"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000002", "A000000001"]


async def test_accepted_unbound_scan3_retires_previous_eligible_scan4(roller_current):
    flow = roller_current
    await flow.current_bins(1)
    old = (await flow.current_rows("bin_return"))[0]
    for n in (2, 1):
        await flow.scan("SCAN3", f"A{n:09}-B", timestamp=100 + n)
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        previous = await flow.roller.original_record(session, "bin_return", old["id"])
        assert not previous["is_current"]
        assert previous["scan4_event_id"] == old["scan4_event_id"]
