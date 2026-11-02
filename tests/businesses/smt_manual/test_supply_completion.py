import pytest
from sqlalchemy import delete, update


async def test_unstarted_withdrawal_closes_demand_without_feed_success(manual_completion_tools):
    flow = manual_completion_tools
    await flow.ready(direction="inbound")
    await flow.bin_plan(cancel=True)
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id="T1"
        )
        assert not progress[0].feed_complete and not progress[0].pending
    await flow.confirm()
    assert len(await flow.confirmations()) == 1


async def test_started_withdrawal_keeps_original_pending(manual_supply):
    flow = manual_supply
    await flow.ready()
    await flow.bin_plan()
    demand_id = await flow.start_supply()
    await flow.withdraw_supply()
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id="T1"
        )
        assert progress[0].pending and not progress[0].feed_complete
    assert await flow.rows("feed", component=True) == []
    await flow.confirm()
    assert await flow.confirmations() == []
    async with flow.factory() as session:
        pending = await flow.roller.list_pending(
            session, kind="FEED", upper_id=demand_id, after_id=None, limit=100
        )
        assert pending[0].record_id == demand_id


async def test_completed_started_withdrawal_uses_actual_progress(manual_supply):
    flow = manual_supply
    await flow.ready()
    await flow.bin_plan()
    demand_id = await flow.start_supply(feed=True)
    await flow.withdraw_supply()
    await flow.confirm()
    assert await flow.confirmations() == []
    await flow.feed_success(demand_id)
    async with flow.factory() as session:
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id="T1"
        )
        assert progress[0].feed_complete and not progress[0].pending
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    assert (await flow.rows("member"))[0]["status"] == "CANCELLED"
    assert (await flow.rows("rack_cycle", component=True))[0]["pending"]


@pytest.mark.parametrize("missing", ["progress", "withdrawal", "association", "orphan"])
async def test_missing_progress_or_withdrawal_evidence_never_implies_completion(
    manual_completion_tools, missing
):
    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    async with flow.factory.begin() as session:
        faces = flow.roller_tables["face_work"]
        if missing == "progress":
            await session.execute(delete(faces).where(faces.c.purpose == "SUPPLY"))
        elif missing == "withdrawal":
            await session.execute(update(flow.tables["member"]).values(cancelled_operation_id=None))
        elif missing == "association":
            await session.execute(
                update(faces).where(faces.c.purpose == "SUPPLY").values(task_id="OTHER")
            )
        else:
            await session.execute(delete(flow.tables["member"]))
    await flow.confirm()
    assert await flow.confirmations() == []
