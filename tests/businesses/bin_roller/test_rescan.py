"""New point attempts retain the original round and physical order."""


async def test_failed_rescan_keeps_first_fifo_order(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    async with flow.factory() as session:
        first = (await flow.rows(session, "return_round"))[0]
        arrival = next(a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4")
    await flow.result(arrival["command_code"], "SCAN4", status="FAILED")
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN4", "A000000002-B", timestamp=21)
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    async with flow.factory() as session:
        attempts = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"]
    for a in attempts[1:]:
        await flow.result(a["command_code"], "SCAN4")
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=1)
        assert [r["bin_code"] for r in candidates] == ["A000000001", "A000000002"]
        current = next(
            r for r in await flow.rows(session, "return_round") if r["id"] == first["id"]
        )
        assert current["scan4_arrival_id"] == first["scan4_arrival_id"]
