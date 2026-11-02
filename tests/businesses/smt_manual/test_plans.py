import asyncio
import json

import pytest
from sqlalchemy import update

from app.models import WorkLine


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_manual_plan_resources_follow_original_direction(manual_flow, direction):
    flow = manual_flow
    await flow.ready(direction=direction)
    first = dict(
        task_id="T1",
        plan_revision=1,
        added_bin_source_racks=[dict(rack_id="R1", rack_face=["A", "B"])],
    )
    if direction == "outbound":
        first.update(
            target_rack=dict(rack_id="TARGET", rack_face="A"),
            added_direct_picks=[
                dict(
                    source_locator=dict(
                        type="RACK_SLOT", rack_id="DIRECT", rack_face="A", slot_id="01"
                    )
                )
            ],
        )
    raw = flow.message("plan_delta", first)
    assert (await flow.deliver(raw)).status_code == 202
    assert (await flow.deliver(raw)).body["code"] == "DUPLICATE"
    tasks, members, demands = (
        await flow.rows("task"),
        await flow.rows("member"),
        await flow.rows("supply_demand", component=True),
    )
    assert tasks[0]["status"] == "EXECUTING" and tasks[0]["plan_revision"] == 1
    assert len(members) == (2 if direction == "inbound" else 3)
    assert [(r["plan_revision"], r["rack_face"]) for r in demands] == [(1, "A"), (1, "B")]
    assert {r["change_id"] for r in demands} == {__import__("json").loads(raw)["operation_id"]}
    invalid = [
        dict(target_rack=dict(rack_id="X", rack_face="A")),
        dict(added_source_racks=[dict(rack_id="X")]),
        dict(added_return_racks=[dict(rack_id="X", rack_faces=["A"])]),
    ]
    if direction == "inbound":
        invalid.append(
            dict(
                added_direct_picks=[
                    dict(
                        source_locator=dict(
                            type="RACK_SLOT", rack_id="X", rack_face="A", slot_id="01"
                        )
                    )
                ]
            )
        )
    for fields in invalid:
        response = await flow.deliver(
            flow.message("plan_delta", dict(task_id="T1", plan_revision=2, **fields))
        )
        assert response.status_code == 422
    second = dict(
        task_id="T1", plan_revision=2, added_bin_source_racks=[dict(rack_id="R1", rack_face=["A"])]
    )
    assert (await flow.deliver(flow.message("plan_delta", second))).status_code == 202
    assert len(await flow.rows("supply_demand", component=True)) == 3


async def test_direct_pick_only_is_a_valid_first_plan(manual_flow):
    flow = manual_flow
    await flow.ready()
    data = dict(
        task_id="T1",
        plan_revision=1,
        added_direct_picks=[
            dict(source_locator=dict(type="RACK_SLOT", rack_id="D", rack_face="A", slot_id="01"))
        ],
    )
    assert (await flow.deliver(flow.message("plan_delta", data))).status_code == 202
    assert await flow.rows("supply_demand", component=True) == []
    assert (await flow.rows("task"))[0]["target_rack"] is None


async def test_pending_plan_requires_original_retry(manual_flow):
    flow = manual_flow
    await flow.issue()
    await flow.prepare()
    first = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R1", rack_face=["A"])],
        ),
    )
    second = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=2,
            added_bin_source_racks=[dict(rack_id="R2", rack_face=["B"])],
        ),
    )
    assert (await flow.deliver(first)).status_code == 503
    assert await flow.rows("member") == []
    await flow.accept_prepare()
    assert await flow.rows("member") == []
    missing = await flow.deliver(second)
    assert missing.status_code == 409 and missing.body["data"] == {
        "reason_code": "REVISION_CONFLICT"
    }
    assert (await flow.deliver(first)).status_code == 202
    assert (await flow.rows("task"))[0]["plan_revision"] == 1
    assert (await flow.deliver(second)).status_code == 202
    assert (await flow.deliver(first)).body["code"] == "DUPLICATE"


@pytest.mark.parametrize("manual_sessions", ["sqlite", "postgres"], indirect=True)
@pytest.mark.parametrize("cancel_task", ["T1", "T2"])
async def test_distinct_operations_keep_original_pending_plan_recoverable(manual_flow, cancel_task):
    flow = manual_flow
    await flow.ready()
    if cancel_task == "T2":
        await flow.ready("T2", line=2)
    subjects = ["T1"] if cancel_task == "T1" else ["T1", "T2"]
    for task_id in subjects:
        assert (
            await flow.deliver(
                flow.message(
                    "plan_delta",
                    dict(
                        task_id=task_id,
                        plan_revision=1,
                        added_bin_source_racks=[dict(rack_id=f"R-{task_id}", rack_face=["A", "B"])],
                    ),
                )
            )
        ).status_code == 202

    plan_data = dict(
        task_id="T1",
        plan_revision=3,
        added_bin_source_racks=[dict(rack_id="RECOVERED", rack_face=["B"])],
    )
    pending = flow.message("plan_delta", plan_data, timestamp=301)
    operation_id = json.loads(pending)["operation_id"]
    assert (await flow.deliver(pending)).status_code == 409
    original_input = dict(
        next(row for row in await flow.rows("input") if row["operation_id"] == operation_id)
    )
    assert original_input["state"] != "APPLIED"
    assert original_input["reason"] == "MISSING_REVISION"
    cancel_data = dict(
        task_id=cancel_task,
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[dict(plan_revision=1, rack_id=f"R-{cancel_task}", rack_face=["A"])],
    )
    cancelled = flow.message("cancel", cancel_data, operation_id=operation_id, timestamp=302)
    assert (await flow.deliver(cancelled)).status_code == 202
    inputs_after_cancel = {
        row["operation"]: dict(row)
        for row in await flow.rows("input")
        if row["operation_id"] == operation_id
    }
    assert (
        await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=2,
                    added_bin_source_racks=[dict(rack_id="PREDECESSOR", rack_face=["A"])],
                ),
            )
        )
    ).status_code == 202
    assert (await flow.deliver(pending)).status_code == 202
    assert inputs_after_cancel["sorting.task.plan_delta@v1"] == original_input
    assert set(inputs_after_cancel) == {"sorting.task.plan_delta@v1", "sorting.task.cancel@v1"}
    original_cancel = inputs_after_cancel["sorting.task.cancel@v1"]
    assert original_cancel["task_id"] == cancel_task
    assert original_cancel["timestamp"] == 302
    assert original_cancel["payload"] == cancel_data
    assert (original_cancel["state"] == "APPLIED") and original_cancel["reason"] is None
    inputs = {
        row["operation"]: dict(row)
        for row in await flow.rows("input")
        if row["operation_id"] == operation_id
    }
    assert inputs["sorting.task.plan_delta@v1"] == dict(
        original_input, state="APPLIED", reason=None, is_plan_record=True
    )
    assert inputs["sorting.task.plan_delta@v1"]["timestamp"] == 301
    assert inputs["sorting.task.plan_delta@v1"]["payload"] == plan_data
    assert inputs["sorting.task.cancel@v1"] == original_cancel
    assert {row["task_id"]: row["plan_revision"] for row in await flow.rows("task")} == {
        task_id: 3 if task_id == "T1" else 1 for task_id in subjects
    }
    demands = await flow.rows("supply_demand", component=True)
    expected = {
        (task_id, 1, f"R-{task_id}", face): task_id == cancel_task and face == "A"
        for task_id in subjects
        for face in ("A", "B")
    }
    expected.update({("T1", 2, "PREDECESSOR", "A"): False, ("T1", 3, "RECOVERED", "B"): False})
    assert {
        (row["task_id"], row["plan_revision"], row["rack_id"], row["rack_face"]): row["withdrawn"]
        for row in demands
    } == expected
    assert next(row for row in demands if row["plan_revision"] == 3)["change_id"] == operation_id
    assert {
        (row["task_id"], row["plan_revision"], row["rack_id"], row["rack_face"]): row["status"]
        for row in await flow.rows("member")
    } == {key: "CANCELLED" if withdrawn else "PENDING" for key, withdrawn in expected.items()}
    assert (await flow.deliver(pending)).body["code"] == "DUPLICATE"
    assert (await flow.deliver(cancelled)).body["code"] == "DUPLICATE"
    assert await flow.rows("supply_demand", component=True) == demands


async def test_current_same_revision_compares_complete_ordered_data(manual_flow):
    flow = manual_flow
    await flow.ready()
    data = dict(
        task_id="T1",
        plan_revision=1,
        added_bin_source_racks=[dict(rack_id="R", rack_face=["A", "B"])],
    )
    assert (await flow.deliver(flow.message("plan_delta", data))).status_code == 202
    assert (await flow.deliver(flow.message("plan_delta", data, timestamp=999))).body[
        "code"
    ] == "DUPLICATE"
    changed = dict(data, added_bin_source_racks=[dict(rack_id="R", rack_face=["B", "A"])])
    assert (await flow.deliver(flow.message("plan_delta", changed))).body["data"] == {
        "reason_code": "REVISION_CONFLICT"
    }
    assert (
        await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=2,
                    added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
                ),
            )
        )
    ).status_code == 202
    assert (await flow.deliver(flow.message("plan_delta", data))).status_code == 409


async def test_terminal_task_replays_history_but_refuses_new_plan_identity(manual_flow):
    flow = manual_flow
    await flow.ready()
    data = dict(
        task_id="T1", plan_revision=1, added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])]
    )
    original = flow.message("plan_delta", data)
    assert (await flow.deliver(original)).status_code == 202
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    assert (await flow.deliver(original)).body["code"] == "DUPLICATE"
    response = await flow.deliver(flow.message("plan_delta", data))
    assert response.status_code == 409
    assert response.body["data"] == {"reason_code": "STATE_CONFLICT"}


@pytest.mark.parametrize("pending_reason", ["revision", "disabled"])
async def test_pending_plan_can_become_a_stable_definite_conflict(
    manual_flow, monkeypatch, pending_reason
):
    from app.integrations.wms_callback import receiver

    flow = manual_flow
    await flow.ready()
    revision = 2 if pending_reason == "revision" else 1
    data = dict(
        task_id="T1",
        plan_revision=revision,
        added_bin_source_racks=[dict(rack_id="ORIGINAL", rack_face=["A"])],
    )
    if pending_reason == "disabled":
        async with flow.factory.begin() as session:
            await session.execute(
                update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
            )
    original = flow.message("plan_delta", data)
    assert (await flow.deliver(original)).status_code == 409
    if pending_reason == "revision":
        for version in (1, 2):
            assert (
                await flow.deliver(
                    flow.message(
                        "plan_delta",
                        dict(
                            task_id="T1",
                            plan_revision=version,
                            added_bin_source_racks=[dict(rack_id="OTHER", rack_face=["A"])],
                        ),
                    )
                )
            ).status_code == 202
    else:
        assert (
            await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
        ).status_code == 202
    monkeypatch.setattr(receiver, "unix_ms", lambda _: 1111)
    definite = await flow.deliver(original)
    assert definite.status_code == 409
    monkeypatch.setattr(receiver, "unix_ms", lambda _: 2222)
    assert (await flow.deliver(original)).body == definite.body


async def test_plan_recheck_keeps_manual_changes_atomic(manual_flow):
    flow = manual_flow
    await flow.issue()
    await flow.prepare()
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(raw)).status_code == 503
    await flow.accept_prepare()
    flow.notices.clear()
    assert (await flow.deliver(raw, rollback=True)).status_code == 202
    assert await flow.rows("member") == []
    assert await flow.rows("supply_demand", component=True) == []
    assert flow.notices == []
    results = await asyncio.gather(flow.deliver(raw), flow.deliver(raw))
    assert sorted(r.body["code"] for r in results) == ["DUPLICATE", "RECEIVED"]
    assert len(await flow.rows("member")) == 1
    assert len(await flow.rows("supply_demand", component=True)) == 1
    assert flow.notices == ["smt_manual"]


async def test_disabled_workline_preserves_original_obligations(manual_flow):
    flow = manual_flow
    await flow.issue()
    await flow.prepare()
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    assert (await flow.issue("NEW")).body["data"] == {"reason_code": "STATE_CONFLICT"}
    # A response already sent under the original preparation remains consumable.
    await flow.accept_prepare()
    assert (await flow.rows("task"))[0]["prepare_accepted"]
    async with flow.factory() as session:
        assert not (
            await flow.ports.read_current_task(session, work_line_id=flow.lines[0])
        ).is_valid
    raw = flow.message(
        "plan_delta",
        dict(
            task_id="T1",
            plan_revision=1,
            added_bin_source_racks=[dict(rack_id="R", rack_face=["A"])],
        ),
    )
    assert (await flow.deliver(raw)).body["data"] == {"reason_code": "STATE_CONFLICT"}
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=True)
        )
    assert await flow.rows("member") == []
    assert (await flow.deliver(raw)).status_code == 202
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    assert (await flow.deliver(raw)).body["code"] == "DUPLICATE"
    cancel = dict(
        task_id="T1",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[dict(plan_revision=1, rack_id="R", rack_face=["A"])],
    )
    assert (await flow.deliver(flow.message("cancel", cancel))).status_code == 202
    assert await flow.prepare() is None
