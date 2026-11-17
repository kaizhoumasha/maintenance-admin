import pytest
from sqlalchemy import delete, update

from app.businesses._components.bin_roller.ports import RollerWorkRef


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
            session, kind="FACE", upper_id=demand_id, after_id=None, limit=100
        )
        assert pending[0].record_id == demand_id


@pytest.mark.parametrize(
    ("rotate", "late_arrival"),
    [(False, False), (True, False), (False, True)],
    ids=["entry", "rotation", "newer-arrival"],
)
async def test_withdrawn_face_needs_final_cancelled_preparation_for_confirmation(
    manual_production_supply, rotate, late_arrival
):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan()
    demand = await flow.start_supply(feed=rotate)
    (cycle,) = await flow.rows("rack_cycle", component=True)
    revision, face = (2, "B") if rotate else (1, "A")
    if rotate:
        assert (
            await flow.deliver(
                flow.message(
                    "plan_delta",
                    dict(
                        task_id="T1",
                        plan_revision=revision,
                        added_bin_source_racks=[dict(rack_id="R", rack_face=[face])],
                    ),
                )
            )
        ).status_code == 202
        await flow.feed_success(demand)
        assert (await flow.scan("A000000001-C", timestamp=10)).status_code == 200
        (box,) = await flow.rows("manual_bin")
        await flow.decision(dict(result="NO_WORK"))
        await flow.advance("bin", box["arrival_id"])
        async with flow.factory.begin() as session:
            await flow.roller.advance(session, RollerWorkRef("RACK_CYCLE", cycle["id"]))
    action = next(
        a for a in await flow.rows("rack_action", component=True) if a["target_face"] == face
    )
    await flow.acknowledge(action)
    assert (
        await flow.deliver(
            flow.message(
                "cancel",
                dict(
                    task_id="T1",
                    cancel_scope="PLAN_MEMBERS",
                    bin_source_racks=[dict(plan_revision=revision, rack_id="R", rack_face=[face])],
                ),
            )
        )
    ).status_code == 202
    await flow.confirm()
    assert await flow.confirmations() == []

    assert (
        await flow.rack_outcome(action, status="CANCELLED", location="FIVE" if rotate else "STORE")
    ).status_code == 202
    async with flow.factory.begin() as session:
        await flow.roller.advance(session, RollerWorkRef("RACK_CYCLE", cycle["id"]))
    if late_arrival:
        assert (await flow.rack_outcome(action, face=face, revision=2)).status_code == 202
        await flow.confirm()
        assert await flow.confirmations() == []
        return

    await flow.confirm()
    confirmations = await flow.confirmations()
    assert len(confirmations) == 1
    request = next(r for r in await flow.rows("request") if r["message_id"] == confirmations[0].id)
    assert request["request_snapshot"] == dict(task_id="T1", last_applied_plan_revision=revision)
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


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
    assert await flow.confirmations() == []
    assert (await flow.rows("member"))[0]["status"] == "CANCELLED"
    assert (await flow.rows("rack_cycle", component=True))[0]["pending"]


@pytest.mark.parametrize("count", [1, 2])
async def test_all_supplied_bins_must_close_production_before_confirmation(
    manual_production_supply, count
):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan()
    demand_id = await flow.start_supply(feed=True)
    await flow.withdraw_supply()
    await flow.feed_success(demand_id, count=count)
    await flow.confirm()
    assert await flow.confirmations() == []
    for index in range(count):
        assert (await flow.scan(f"A{index + 1:09}-C", timestamp=10 + index)).status_code == 200
        arrival = (await flow.rows("manual_bin"))[-1]["arrival_id"]
        await flow.decision(dict(result="NO_WORK"))
        await flow.advance("bin", arrival)
        await flow.confirm()
        assert len(await flow.confirmations()) == (1 if index + 1 == count else 0)
    original = (await flow.rows("manual_bin"))[-1]
    await flow.result(original["command_code"])
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


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
                update(faces).where(faces.c.purpose == "SUPPLY").values(scope_id="OTHER")
            )
        else:
            await session.execute(delete(flow.tables["member"]))
    await flow.confirm()
    assert await flow.confirmations() == []
