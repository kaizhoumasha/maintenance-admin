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
        table = flow.store.tables["scan_arrival"]
        await session.execute(
            table.update()
            .where(table.c.id == first["scan3_arrival_id"])
            .values(return_state=head_state)
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


async def test_first_scan4_arrival_orders_candidates_even_when_scan3_order_differs(roller_scans):
    flow = roller_scans
    for n in (1, 2):
        await flow.scan("SCAN3", f"A{n:09}-B", timestamp=n)
    for n in (2, 1):
        await flow.scan("SCAN4", f"A{n:09}-B", timestamp=10 + n)
    async with flow.factory() as session:
        arrivals = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"]
    for arrival in arrivals:
        await flow.result(arrival["command_code"], "SCAN4")
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=1)
        assert [r["bin_code"] for r in candidates] == ["A000000002", "A000000001"]


async def test_accepted_unbound_scan3_retires_previous_eligible_scan4(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    async with flow.factory() as session:
        fourth = (await flow.rows(session, "scan_arrival"))[-1]
    await flow.result(fourth["command_code"], "SCAN4")
    async with flow.factory.begin() as session:
        assert [
            r["id"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == [fourth["round_id"]]
        repo = flow.roller.repository(1)
        # The receiver's local visit is durable even before its round is linked.
        for i in (2, 1):
            latest = await repo.accept_scan_fact(
                session,
                digest=f"later-{i}",
                receipt_id=30 + i,
                point="SCAN3",
                device_code=flow.devices["SCAN3"],
                raw_bin_code=f"A{i:09}-B",
                bin_code=f"A{i:09}",
                scanned_at=30 + i,
            )
        assert latest["round_id"] is None
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        old = await flow.roller.original_record(session, "return_round", fourth["round_id"])
        assert old["return_state"] == "PENDING" and old["first_scan4_arrival_id"] == fourth["id"]
