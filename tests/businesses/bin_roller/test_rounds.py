"""Original round removal fences remain independent of later physical visits."""


async def test_removed_round_does_not_block_new_round(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    async with flow.factory.begin() as session:
        original = (await flow.rows(session, "return_round"))[0]
        table = flow.store.tables["return_round"]
        await session.execute(
            table.update().where(table.c.id == original["id"]).values(removed=True)
        )
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN3", "A000000001-B", timestamp=30)
    await flow.scan("SCAN4", "A000000002-B", timestamp=31)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory() as session:
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)
        rounds = [
            r for r in await flow.rows(session, "return_round") if r["bin_code"] == "A000000001"
        ]
        assert len(rounds) == 2
        assert next(r for r in rounds if r["id"] != original["id"])["status"] == "PENDING"


async def test_old_result_cannot_reopen_new_round(roller_return):

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        opportunity = await flow.selected(session)
        batch_id = await flow.start(session, opportunity)
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
        old = (await flow.rows(session, "return_round"))[0]
        table = flow.store.tables["return_round"]
        await session.execute(table.update().where(table.c.id == old["id"]).values(removed=True))
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN3", "A000000001-B", timestamp=30)
    await flow.scan("SCAN4", "A000000002-B", timestamp=31)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    await flow.success(batch_id)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        rounds = [
            r for r in await flow.rows(session, "return_round") if r["bin_code"] == "A000000001"
        ]
        original = next(r for r in rounds if r["id"] == old["id"])
        newer = next(r for r in rounds if r["id"] != old["id"])
        assert original["removed"] and original["status"] == old["status"]
        assert newer["status"] == "PENDING" and not newer["removed"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
        assert not await flow.roller.return_channel_empty(session, work_line_id=1)


async def test_completed_round_late_result_cannot_finish_next_round(roller_return):

    flow = roller_return
    await flow.bins(1)
    async with flow.factory.begin() as session:
        batch_id = await flow.start(session, await flow.selected(session))
        await flow.decide(session, batch_id)
        await flow.consume_return(session, batch_id)
    await flow.success(batch_id)
    async with flow.factory.begin() as session:
        await flow.consume_return(session, batch_id)
        old = (await flow.rows(session, "return_round"))[0]
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN3", "A000000001-B", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory.begin() as session:
        assert await flow.consume_return(session, batch_id) == "SUCCEEDED"
        rounds = [
            r for r in await flow.rows(session, "return_round") if r["bin_code"] == "A000000001"
        ]
        assert len(rounds) == 2
        newer = next(r for r in rounds if r["id"] != old["id"])
        assert newer["status"] == "PENDING"
        assert newer["scan4_arrival_id"] != old["scan4_arrival_id"]
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
