import pytest

from tests.helpers import set_position_capacity


async def direct_plan(flow, count=5, *, target=None):
    payload = {
        "task_id": "T1",
        "plan_revision": 1,
        "added_direct_picks": [
            {
                "source_locator": {
                    "type": "RACK_SLOT",
                    "rack_id": f"D{i}",
                    "rack_face": "A",
                    "slot_id": "1",
                }
            }
            for i in range(count)
        ],
    }
    if not count:
        payload.pop("added_direct_picks")
    if target:
        payload["target_rack"] = target
    response = await flow.deliver(flow.message("plan_delta", payload))
    assert response.status_code == 202, response.body


async def test_task_candidates_fill_capacity_and_bound_waiter_reenters_manual_pending(manual_host):
    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3, role="RETURN_RACK")
    await direct_plan(flow)
    await flow.host.advance("TASK", "T1")
    jobs = await flow.rows("rack")
    entries = [a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]
    assert len(jobs) == 5
    assert len(entries) == 3
    submitted = {a["rack_work_id"] for a in entries}
    waiter = next(j for j in jobs if j["id"] not in submitted and j["rack_id"] == "D3")
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 4, role="RETURN_RACK")
    await flow.host.advance("MANUAL_RACK", waiter["id"])
    after = [a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]
    assert len(after) == 4
    assert set(a["id"] for a in entries) <= set(a["id"] for a in after)
    assert len([p for p in await flow.rows("plan")]) == 1


async def test_transfer_capacity_never_prepares_or_borrows_from_next_task(manual_host):
    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3, role="TRANSFER_RACK")
    await direct_plan(flow, 0, target={"rack_id": "TARGET", "rack_face": "A"})
    await flow.issue("T2")
    for _ in range(2):
        await flow.host.scan_pending()
    tasks = await flow.rows("task")
    assert next(t for t in tasks if t["task_id"] == "T2")["status"] == "QUEUED"
    assert {j["task_id"] for j in await flow.rows("rack")} == {"T1"}
    assert len([a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]) == 1


async def test_queued_tail_does_not_stop_head_departure(manual_host):
    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3, role="RETURN_RACK")
    await direct_plan(flow, 3)
    await flow.host.advance("TASK", "T1")
    jobs = await flow.rows("rack")
    head = next(j for j in jobs if j["rack_id"] == "D0")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == head["id"])
    await flow.rack_outcome(entry, face="A")
    await flow.host.advance("MANUAL_RACK", head["id"])
    assert (await flow.deliver(flow.notification(rack_id="D0"))).status_code == 202
    assert await flow.host.advance("MANUAL_RACK", head["id"]) == "WAITING_FOR_DEPARTURE"
    assert len(await flow.rows("rack_departure")) == 1


async def test_two_unhanded_off_arrivals_at_one_point_are_a_production_conflict(manual_host):
    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 2, role="RETURN_RACK")
    await direct_plan(flow, 2)
    await flow.host.advance("TASK", "T1")
    entries = [a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]
    for entry in entries:
        await flow.rack_outcome(entry, face="A")
        await flow.host.advance("MANUAL_RACK", entry["rack_work_id"])
    async with flow.factory() as session:
        assert await flow.host.rack_dispatch.has_position_conflict(
            session, point=entries[0]["target"]["location_code"]
        )


async def test_terminal_task_withdraws_only_unsubmitted_candidates(manual_host):
    from sqlalchemy import update

    flow = manual_host
    await flow.ready()
    await direct_plan(flow, 3)
    await flow.host.advance("TASK", "T1")
    entries = [a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]
    assert len(entries) == 1
    original = entries[0]
    async with flow.factory.begin() as session:
        await session.execute(
            update(flow.tables["task"])
            .where(flow.tables["task"].c.task_id == "T1")
            .values(status="COMPLETED")
        )
    jobs = await flow.rows("rack")
    for job in jobs:
        await flow.host.advance("MANUAL_RACK", job["id"])
    after = {j["id"]: j for j in await flow.rows("rack")}
    assert after[original["rack_work_id"]]["pending"]
    assert all(
        j["state"] == "WITHDRAWN" for key, j in after.items() if key != original["rack_work_id"]
    )
    assert [a["id"] for a in await flow.rows("rack_action")] == [original["id"]]


async def test_plan_receipt_only_materializes_candidates_without_reading_transport_results(
    manual_host, monkeypatch
):
    flow = manual_host
    await flow.ready()
    await direct_plan(flow, 1)
    await flow.host.advance("TASK", "T1")
    entry = next(a for a in await flow.rows("rack_action") if a["kind"] == "ENTER")
    await flow.rack_outcome(entry, face="A")
    await flow.host.advance("MANUAL_RACK", entry["rack_work_id"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("receipt must not read Transport results")

    with monkeypatch.context() as guard:
        guard.setattr(flow.host.rack_dispatch.capabilities.transport, "get_result", forbidden)
        response = await flow.deliver(
            flow.message(
                "plan_delta",
                {
                    "task_id": "T1",
                    "plan_revision": 2,
                    "added_direct_picks": [
                        {
                            "source_locator": {
                                "type": "RACK_SLOT",
                                "rack_id": "D0",
                                "rack_face": "A",
                                "slot_id": "2",
                            }
                        }
                    ],
                },
            )
        )
        assert response.status_code == 202


async def test_original_departure_acceptance_refills_one_bound_manual_candidate(manual_host):
    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3, role="RETURN_RACK")
    await direct_plan(flow, 5)
    await flow.host.advance("TASK", "T1")
    head = next(j for j in await flow.rows("rack") if j["rack_id"] == "D0")
    entry = next(a for a in await flow.rows("rack_action") if a["rack_work_id"] == head["id"])
    await flow.rack_outcome(entry, face="A")
    await flow.host.advance("MANUAL_RACK", head["id"])
    await flow.deliver(flow.notification(rack_id="D0"))
    await flow.host.advance("MANUAL_RACK", head["id"])
    await flow.rack_response(
        "rack_departure",
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="STORAGE")),
    )
    await flow.host.advance("MANUAL_RACK", head["id"])
    departure = next(a for a in await flow.rows("rack_action") if a["kind"] == "DEPART")
    await flow.acknowledge(departure)
    await flow.host.advance("MANUAL_RACK", head["id"])
    await flow.host.scan_pending()
    entries = [a for a in await flow.rows("rack_action") if a["kind"] == "ENTER"]
    assert len(entries) == 4
    jobs = {j["id"]: j for j in await flow.rows("rack")}
    assert jobs[head["id"]]["state"] == "COMPLETED"
    assert {jobs[a["rack_work_id"]]["rack_id"] for a in entries} == {"D0", "D1", "D2", "D3"}


async def test_materializing_saved_plans_preserves_revision_before_source_kind(
    manual_host, monkeypatch
):
    from sqlalchemy import select

    from app.models import WorkLine, WorkLinePosition

    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, flow.lines[0])
        old = await session.scalar(
            select(WorkLinePosition).where(
                WorkLinePosition.work_line_id == line.id,
                WorkLinePosition.position_code == line.position_bindings["FIVE_RACK"],
            )
        )
        await session.delete(old)
        line.position_bindings = {
            **line.position_bindings,
            "FIVE_RACK": line.position_bindings["RETURN_RACK"],
        }

    async def unmaterialized(*args, **kwargs):
        pass

    with monkeypatch.context() as legacy:
        legacy.setattr(flow.host, "_register_task_rack_candidates", unmaterialized)
        await direct_plan(flow, 1)
        response = await flow.deliver(
            flow.message(
                "plan_delta",
                {
                    "task_id": "T1",
                    "plan_revision": 2,
                    "added_bin_source_racks": [{"rack_id": "BIN", "rack_face": ["A"]}],
                },
            )
        )
        assert response.status_code == 202
    await flow.host.advance("TASK", "T1")
    async with flow.factory() as session:
        jobs = await flow.host.rack_dispatch.jobs(
            session,
            work_line_id=flow.lines[0],
            source_kinds=("MANUAL_RETURN", "ROLLER_ORDINARY"),
            task_id="T1",
        )
        assert [j.origin.rack_id for j in sorted(jobs, key=lambda j: j.admission_order)] == [
            "D0",
            "BIN",
        ]


@pytest.mark.parametrize("role", ["FIVE_RACK", "RETURN_RACK", "TRANSFER_RACK"])
async def test_formal_sender_delivers_only_current_task_position_capacity(
    formal_manual, monkeypatch, role
):
    from app import business_wakeups

    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3, role=role)
    task = flow.prefix + "-capacity"
    assert (await flow.issue(task, direction="outbound")).status_code == 202
    assert (await flow.issue(task + "-next", direction="outbound")).status_code == 202
    await flow.host.advance("TASK", task)
    await flow.dispatch()
    await flow.host.advance("TASK", task)
    data = dict(task_id=task, plan_revision=1)
    if role == "FIVE_RACK":
        data["added_bin_source_racks"] = [
            dict(rack_id=f"FORMAL-{i}", rack_face=["A"]) for i in range(5)
        ]
    elif role == "RETURN_RACK":
        data["added_direct_picks"] = [
            dict(
                source_locator=dict(
                    type="RACK_SLOT", rack_id=f"FORMAL-{i}", rack_face="A", slot_id="1"
                )
            )
            for i in range(5)
        ]
    else:
        data["target_rack"] = dict(rack_id="FORMAL-TARGET", rack_face="A")
    assert (await flow.callback("sorting.task.plan_delta@v1", data)).status_code == 202
    await flow.host.scan_pending()
    await flow.dispatch()
    actions = await flow.rows("rack_action", roller=role == "FIVE_RACK")
    entries = [a for a in actions if a["kind"] in {"ENTER", "CTU01"}]
    expected = 1 if role == "TRANSFER_RACK" else 3
    assert len(entries) == expected
    original_ids = {a["transport_task_id"] for a in entries}
    delivered = [
        body for _, body in flow.requests if body.get("operation") == "transport.task.submit@v1"
    ]
    assert sorted(body["data"]["transport_task_id"] for body in delivered) == sorted(
        a["transport_task_id"] for a in entries
    )
    assert len({body["data"]["rack_id"] for body in delivered}) == expected
    await flow.host.scan_pending()
    await flow.dispatch()
    assert (
        len(
            [
                body
                for _, body in flow.requests
                if body.get("operation") == "transport.task.submit@v1"
            ]
        )
        == expected
    )
    assert {
        a["transport_task_id"] for a in await flow.rows("rack_action", roller=role == "FIVE_RACK")
    } == original_ids
    tasks = await flow.rows("task")
    assert next(t for t in tasks if t["task_id"] == task + "-next")["prepare_operation_id"] is None


@pytest.mark.parametrize("manual_sessions", ["sqlite"], indirect=True)
async def test_advancing_bound_sources_keeps_face_reads_bounded(manual_host):
    from sqlalchemy import event

    flow = manual_host
    await flow.ready()
    async with flow.factory.begin() as session:
        await set_position_capacity(session, flow.lines[0], 3)
    response = await flow.deliver(
        flow.message(
            "plan_delta",
            {
                "task_id": "T1",
                "plan_revision": 1,
                "added_bin_source_racks": [
                    {"rack_id": f"SOURCE-{number}", "rack_face": ["A"]} for number in range(20)
                ],
            },
        )
    )
    assert response.status_code == 202
    await flow.host.scan_pending()
    async with flow.factory() as session:
        original = await flow.host.rack_dispatch.jobs(
            session, work_line_id=flow.lines[0], source_kinds=("ROLLER_ORDINARY",), task_id="T1"
        )
    identities = {job.id: job.admission_order for job in original}
    demands = await flow.rows("face_work", component=True)
    demand = next(row for row in demands if row["purpose"] == "SUPPLY" and row["rack_job_id"])
    reads = []

    def count_reads(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "face_work" in statement
            and "rack_face =" in statement
        ):
            reads.append(statement)

    engine = flow.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", count_reads)
    try:
        await flow.host.advance("FACE", demand["id"])
    finally:
        event.remove(engine, "before_cursor_execute", count_reads)
    assert len(reads) <= 2, f"bound source advance repeated {len(reads)} face point reads"
    async with flow.factory() as session:
        after = await flow.host.rack_dispatch.jobs(
            session, work_line_id=flow.lines[0], source_kinds=("ROLLER_ORDINARY",), task_id="T1"
        )
    assert {job.id: job.admission_order for job in after} == identities
    assert sum(action.kind == "ENTER" for job in after for action in job.actions) == 3


async def test_reconciliation_keeps_original_preparation_when_entry_was_cancelled(manual_host):
    flow = manual_host
    await flow.ready()
    await direct_plan(flow, 1)
    await flow.host.advance("TASK", "T1")
    entry = next(a for a in await flow.rows("rack_action") if a["kind"] == "ENTER")
    await flow.rack_outcome(entry, status="CANCELLED")
    await flow.host.advance("MANUAL_RACK", entry["rack_work_id"])
    for _ in range(2):
        await flow.host.advance("TASK", "T1")
    member = next(
        m for m in await flow.rows("member") if m["rack_work_id"] == entry["rack_work_id"]
    )
    assert member["preparation_action_id"] == entry["id"]
    assert [a["id"] for a in await flow.rows("rack_action") if a["kind"] == "ENTER"] == [
        entry["id"]
    ]
