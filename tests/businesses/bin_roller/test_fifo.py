"""Physical FIFO heads are checked before eligibility filtering."""

from datetime import UTC, datetime

import pytest


@pytest.mark.parametrize("head", ["unready", "unknown"])
async def test_pending_head_is_not_filtered_out(roller_current, head):
    flow = roller_current
    await flow.scan("SCAN3", "A000000001-B", timestamp=1)
    await flow.scan("SCAN4", "A000000001-B", timestamp=2)
    if head == "unknown":
        row = (await flow.current_rows("bin_return"))[0]
        await flow.result(row["scan4_command_code"], "SCAN4", status="FAILED")
    await flow.current_bins(1, offset=1)
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)


@pytest.mark.parametrize("raw", ["NOREAD", "A000000001-C"])
async def test_invalid_scan4_stays_outside_fifo(roller_current, raw):
    flow = roller_current
    await flow.scan("SCAN4", raw, timestamp=1)
    orphan = (await flow.current_rows("bin_return"))[0]
    assert orphan["scan4_event_id"] and orphan["return_state"] == "OBSERVED"
    async with flow.factory() as session:
        assert await flow.roller.return_channel_empty(session, work_line_id=1)
    await flow.current_bins(1, offset=1)
    async with flow.factory() as session:
        assert [
            r["bin_code"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000002"]


@pytest.mark.parametrize("raw", ["NOREAD", "A000000001-C"])
async def test_legacy_invalid_scan4_does_not_hold_back_candidates(roller_current, raw):
    flow = roller_current
    await flow.scan("SCAN4", raw, timestamp=1)
    orphan = (await flow.current_rows("bin_return"))[0]
    async with flow.factory.begin() as session:
        await flow.roller.repository(1).update(
            session,
            "bin_return",
            orphan["id"],
            first_scan4_event_id=orphan["scan4_event_id"],
            first_scan4_received_at=datetime.now(UTC),
            fifo_visit_key=orphan["scan4_visit_key"],
        )
        assert await flow.roller.return_channel_empty(session, work_line_id=1)
    await flow.current_bins(1)
    async with flow.factory() as session:
        assert [
            row["bin_code"]
            for row in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000001"]


@pytest.mark.parametrize("existing_key", [False, True], ids=["new", "existing"])
async def test_unmatched_valid_scan4_blocks_fifo_until_replaced(roller_current, existing_key):
    flow = roller_current
    await flow.scan("SCAN4", "A000000001-B", timestamp=1)
    observed = (await flow.current_rows("bin_return"))[0]
    assert observed["return_state"] == "OBSERVED" and observed["scan4_command_code"] is None
    if existing_key:
        async with flow.factory.begin() as session:
            await flow.roller.repository(1).update(
                session,
                "bin_return",
                observed["id"],
                first_scan4_event_id=observed["scan4_event_id"],
                first_scan4_received_at=datetime.now(UTC),
                fifo_visit_key=observed["scan4_visit_key"],
            )
    else:
        assert observed["first_scan4_event_id"] == observed["scan4_event_id"]
    async with flow.factory() as session:
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)
    await flow.current_bins(1, offset=1)
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()

    await flow.scan("SCAN3", "A000000001-B", timestamp=30)
    async with flow.factory() as session:
        assert [
            row["bin_code"]
            for row in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == ["A000000002"]
        old = await flow.roller.original_record(session, "bin_return", observed["id"])
        assert old["close_reason"] == "SUPERSEDED"


@pytest.mark.parametrize(
    ("kind", "outcome"), [("FACE_END", "WAITING_FOR_FIFO_HEAD"), ("SEGMENT", "WINDOW_ENDED")]
)
async def test_return_windows_respect_unmatched_physical_head(roller_return, kind, outcome):
    flow = roller_return
    await flow.scan("SCAN4", "A000000001-B", timestamp=1)
    await flow.bins(1, offset=1)
    async with flow.factory.begin() as session:
        face_id = await flow.selected(session, kind=kind)
        assert await flow.consume_return(session, face_id) == outcome
        face = await flow.roller.original_record(session, "face_work", face_id)
        assert face["return_message_id"] is None


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
