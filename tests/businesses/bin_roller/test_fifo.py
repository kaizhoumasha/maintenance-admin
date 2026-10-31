"""Physical head obligations precede eligibility filtering."""

import pytest


@pytest.mark.parametrize("head_state", ["PENDING", "ALLOCATING", "IN_TRANSIT"])
async def test_pending_head_is_not_filtered_out(roller_scans, head_state):
    flow = roller_scans
    for i in (1, 2):
        await flow.scan("SCAN3", f"A{i:09}-B", timestamp=i * 10)
        await flow.scan("SCAN4", f"A{i:09}-B", timestamp=i * 10)
    async with flow.factory.begin() as session:
        arrivals = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"]
        rounds = await flow.rows(session, "return_round")
        first = next(r for r in rounds if r["bin_code"] == "A000000001")
        table = flow.store.tables["return_round"]
        await session.execute(
            table.update().where(table.c.id == first["id"]).values(status=head_state)
        )
    await flow.result(arrivals[1]["command_code"], "SCAN4")
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()


async def test_nonempty_prefix_does_not_wait_for_four_bins(roller_scans):
    flow = roller_scans
    for i in (1, 2, 3):
        await flow.scan("SCAN3", f"A{i:09}-B", timestamp=i * 10)
        await flow.scan("SCAN4", f"A{i:09}-B", timestamp=i * 10)
    async with flow.factory() as session:
        arrivals = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"]
    for arrival in (arrivals[0], arrivals[2]):
        await flow.result(arrival["command_code"], "SCAN4")
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=1)
        assert [r["bin_code"] for r in candidates] == ["A000000001"]
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)


async def test_unassociated_physical_head_is_not_filtered_out(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN4", "A000000001-B", timestamp=10)
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN4", "A000000002-B", timestamp=21)
    async with flow.factory() as session:
        second = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"][-1]
    await flow.result(second["command_code"], "SCAN4")
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)


async def test_unknown_same_point_does_not_resend(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    await flow.scan("SCAN4", "A000000002-B", timestamp=12)
    await flow.scan("SCAN4", "A000000001-B", timestamp=13)
    async with flow.factory() as session:
        attempts = [
            a
            for a in await flow.rows(session, "scan_arrival")
            if a["point"] == "SCAN4" and a["bin_code"] == "A000000001"
        ]
        assert len(attempts) == 2
        assert len([a for a in attempts if a["command_code"]]) == 1
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
