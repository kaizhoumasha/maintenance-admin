from sqlalchemy import update


async def test_supply_change_and_member_cancel_commit_together(manual_flow):
    flow = manual_flow
    await flow.ready(direction="inbound")
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A", "B"])],
        ),
    )
    assert (await flow.deliver(raw, rollback=True)).status_code == 202
    assert await flow.rows("member") == await flow.rows("supply_demand", component=True) == []
    assert (await flow.deliver(raw)).status_code == 202
    cancelled = flow.message(
        "cancel",
        dict(
            task_id="T1",
            cancel_scope="PLAN_MEMBERS",
            bin_source_racks=[dict(plan_revision=1, rack_id="R", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(cancelled, rollback=True)).status_code == 202
    assert all(not row["withdrawn"] for row in await flow.rows("supply_demand", component=True))
    assert (await flow.deliver(cancelled)).status_code == 202
    demands = await flow.rows("supply_demand", component=True)
    assert {r["rack_face"]: r["withdrawn"] for r in demands} == {"A": True, "B": False}
    assert (await flow.rows("task"))[0]["plan_revision"] == 1
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 409


async def test_exact_member_cancellation_skips_only_requested_members(manual_flow):
    flow = manual_flow
    await flow.ready()

    def picks(slots):
        return [
            dict(source_locator=dict(type="RACK_SLOT", rack_id="D", rack_face="A", slot_id=slot))
            for slot in slots
        ]

    for revision, slots in ((1, ["01", "02", "03"]), (2, ["01"])):
        assert (
            await flow.deliver(
                flow.message(
                    "plan_delta",
                    dict(task_id="T1", plan_revision=revision, added_direct_picks=picks(slots)),
                )
            )
        ).status_code == 202
    member = flow.tables["member"]
    async with flow.factory.begin() as session:
        await session.execute(
            update(member)
            .where(member.c.plan_revision == 1, member.c.slot_id == "03")
            .values(status="STARTED")
        )
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))

    def cancel(revision=1, **fields):
        return flow.message(
            "cancel",
            dict(
                task_id="T1",
                cancel_scope="PLAN_MEMBERS",
                direct_pick_sources=[
                    dict(plan_revision=revision, rack_id="D", rack_face="A", **fields)
                ],
            ),
        )

    assert (await flow.deliver(cancel(slot_ids=["01", "unknown"]))).status_code == 202
    rows = await flow.rows("member")
    assert {(r["plan_revision"], r["slot_id"]): r["status"] for r in rows} == {
        (1, "01"): "CANCELLED",
        (1, "02"): "PENDING",
        (1, "03"): "STARTED",
        (2, "01"): "PENDING",
    }
    assert (await flow.deliver(cancel())).status_code == 202
    assert (await flow.deliver(cancel())).status_code == 202
    assert (await flow.deliver(cancel(revision=99))).status_code == 202
    rows = await flow.rows("member")
    assert {(r["plan_revision"], r["slot_id"]): r["status"] for r in rows} == {
        (1, "01"): "CANCELLED",
        (1, "02"): "CANCELLED",
        (1, "03"): "STARTED",
        (2, "01"): "PENDING",
    }
    task = (await flow.rows("task"))[0]
    assert task["status"] == "COMPLETED" and task["plan_revision"] == 2


async def test_task_cancel_does_not_reopen_on_late_prepare(manual_flow):
    flow = manual_flow
    await flow.issue()
    await flow.prepare()
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    assert await flow.accept_prepare() == "CANCELLED"
    assert (await flow.issue()).status_code == 409
