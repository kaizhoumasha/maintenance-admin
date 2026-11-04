"""One formal manual business owns independent task/arrival/FIFO scopes per line."""

import asyncio

from sqlalchemy import select

from app import business_wakeups
from app.models import TransportTask
from tests.helpers import manual_source_departure


async def test_two_lines_keep_tasks_arrivals_and_returns_separate(formal_manual, monkeypatch):
    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    tasks = [flow.prefix + "-in", flow.prefix + "-out"]
    for line, direction in enumerate(("inbound", "outbound")):
        assert (await flow.issue(tasks[line], line=line, direction=direction)).status_code == 202
        assert (
            await flow.issue(
                tasks[line] + "-queued",
                line=line,
                direction="outbound" if direction == "inbound" else "inbound",
            )
        ).status_code == 202
        await flow.host.advance("TASK", tasks[line])
    await flow.dispatch()
    for task in tasks:
        await flow.host.advance("TASK", task)
    for line in (0, 1):
        assert (
            await flow.callback(
                "sorting.task.plan_delta@v1",
                dict(
                    task_id=tasks[line],
                    plan_revision=1,
                    added_bin_source_racks=[dict(rack_id=f"SOURCE-{line}", rack_face=["A"])],
                ),
            )
        ).status_code == 202
    demands = await flow.rows("supply_demand", roller=True)
    assert {(r["work_line_id"], r["task_id"], r["rack_id"]) for r in demands} == {
        (flow.lines[0], tasks[0], "SOURCE-0"),
        (flow.lines[1], tasks[1], "SOURCE-1"),
    }
    for demand in demands:
        await flow.host.advance("FEED", demand["id"])
    actions = await flow.rows("rack_action", roller=True)
    assert {(r["work_line_id"], r["target"]["location_code"]) for r in actions} == {
        (flow.lines[0], flow.positions[0]["FIVE_RACK"]),
        (flow.lines[1], flow.positions[1]["FIVE_RACK"]),
    }
    assert (
        await flow.callback(
            "sorting.task.cancel@v1",
            dict(
                task_id=tasks[0],
                cancel_scope="PLAN_MEMBERS",
                bin_source_racks=[dict(plan_revision=1, rack_id="SOURCE-0", rack_face=["A"])],
            ),
        )
    ).status_code == 202
    async with flow.factory() as session:
        original_progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id=tasks[0]
        )
        assert len(original_progress) == 1 and original_progress[0].pending
        assert original_progress[0].source.rack_id == "SOURCE-0"
        progress = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[1], task_id=tasks[1]
        )
        assert len(progress) == 1 and progress[0].source.rack_id == "SOURCE-1"
    demands_after = await flow.rows("supply_demand", roller=True)
    assert {(r["work_line_id"], r["withdrawn"]) for r in demands_after} == {
        (flow.lines[0], True),
        (flow.lines[1], False),
    }
    assert [
        (r["id"], r["transport_task_id"]) for r in await flow.rows("rack_action", roller=True)
    ] == [(r["id"], r["transport_task_id"]) for r in actions]
    for line in (0, 1):
        assert (await flow.scan("SCAN2", line=line)).status_code == 200
    work = await flow.rows("manual_bin")
    assert {(row["work_line_id"], row["task_id"], row["bin_code"]) for row in work} == {
        (flow.lines[0], tasks[0], "A000000001"),
        (flow.lines[1], tasks[1], "A000000001"),
    }
    await flow.dispatch()
    for row in work:
        await flow.host.advance("MANUAL_BIN", row["arrival_id"])
    released = await flow.rows("manual_bin")
    assert len({row["command_code"] for row in released}) == 2
    for line in (0, 1):
        await flow.scan("SCAN3", "A000000001-B", line=line, timestamp=20)
        await flow.scan("SCAN4", "A000000001-B", line=line, timestamp=21)
    await flow.dispatch()
    arrivals = await flow.rows("scan_arrival", roller=True)
    scans4 = [row for row in arrivals if row["point"] == "SCAN4"]
    first = next(row for row in scans4 if row["work_line_id"] == flow.lines[0])
    second = next(row for row in scans4 if row["work_line_id"] == flow.lines[1])
    await flow.result(first["command_code"])
    await flow.host.advance("SCAN", first["id"])
    async with flow.factory() as session:
        ready = await flow.roller.get_return_candidates(session, work_line_id=flow.lines[0])
        assert [row["scan4_arrival_id"] for row in ready] == [first["id"]]
        assert await flow.roller.get_return_candidates(session, work_line_id=flow.lines[1]) == ()
    await flow.result(second["command_code"])
    await flow.host.advance("SCAN", second["id"])
    await flow.result(first["command_code"])
    async with flow.factory() as session:
        ready = await flow.roller.get_return_candidates(session, work_line_id=flow.lines[1])
        assert [row["scan4_arrival_id"] for row in ready] == [second["id"]]
    from tests.helpers import manual_return_opportunity

    for line in (0, 1):
        await manual_return_opportunity(flow, line=line)
    batches = await flow.rows("return_batch", roller=True)
    assert {
        (r["work_line_id"], r["caller_workline_id"], tuple(m["round_id"] for m in r["members"]))
        for r in batches
    } == {
        (flow.lines[0], str(flow.lines[0]), (first["round_id"],)),
        (flow.lines[1], str(flow.lines[1]), (second["round_id"],)),
    }
    assert all(row["owner_domain"] == "smt_manual" for row in arrivals)
    assert {
        row["status"] for row in await flow.rows("task") if row["task_id"].endswith("-queued")
    } == {"QUEUED"}
    assert flow.devices[0] != flow.devices[1] and flow.positions[0] != flow.positions[1]


async def test_each_line_refills_one_old_waiter_per_original_departure_acceptance(
    formal_manual,
    monkeypatch,
):
    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    async with flow.factory.begin() as session:
        from tests.helpers import set_position_capacity

        for line_id in flow.lines:
            await set_position_capacity(session, line_id, 3)
    for line in (0, 1):
        task = flow.prefix + f"-capacity-{line}"
        assert (await flow.issue(task, line=line)).status_code == 202
        await flow.host.advance("TASK", task)
        await flow.dispatch()
        await flow.host.advance("TASK", task)
        assert (
            await flow.callback(
                "sorting.task.plan_delta@v1",
                dict(
                    task_id=task,
                    plan_revision=1,
                    added_bin_source_racks=[
                        dict(rack_id=f"SOURCE-{line}-{i}", rack_face=["A"]) for i in range(5)
                    ],
                ),
            )
        ).status_code == 202
    await flow.host.scan_pending()
    await flow.dispatch()

    async def entry_counts():
        actions = await flow.rows("rack_action", roller=True)
        return [
            sum(a["kind"] == "CTU01" and a["work_line_id"] == line for a in actions)
            for line in flow.lines
        ]

    assert await entry_counts() == [3, 3]
    original = [
        r
        for r in await flow.rows("rack_action", roller=True)
        if r["kind"] == "CTU01" and r["work_line_id"] == flow.lines[0]
    ]
    for index, expected in ((0, [4, 3]), (1, [5, 3])):
        departure = await manual_source_departure(flow, original[index])
        await flow.dispatch()
        assert flow.notices[-1] == "smt_manual"
        await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
        # First sweep can encounter the old waiter before consuming the original CTU03.
        await flow.host.scan_pending()
        await flow.dispatch()
        assert await entry_counts() == expected
        cycles = await flow.rows("rack_cycle", roller=True)
        cycle = next(c for c in cycles if c["id"] == original[index]["cycle_id"])
        basis = cycle["release_basis"]
        assert cycle["status"] == "RELEASED"
        assert basis["response_code"] == "RECEIVED"
        async with flow.factory() as session:
            transport = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == departure["transport_task_id"],
                )
            )
            assert transport.applied_outcome is None
        await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
        assert await entry_counts() == expected
        assert (
            next(c for c in await flow.rows("rack_cycle", roller=True) if c["id"] == cycle["id"])[
                "release_basis"
            ]
            == basis
        )
    print("capacity cumulative CTU01: 3/3 -> 4/3 -> 5/3; both CTU03 without final Result")
