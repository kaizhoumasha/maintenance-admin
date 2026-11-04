"""Supplied KT16/KT18 acceptance; every business transition uses current product code."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Device, DeviceRoute, EcsCommand, TransportTask, WorkLine
from tests.businesses.smt_manual.factory_profile import PROFILE
from tests.helpers import manual_return_opportunity


async def ready(flow, *, line=0):
    task = flow.prefix + "-" + PROFILE["lines"][line]["code"]
    assert (await flow.issue(task, line=line, direction="outbound")).status_code == 202
    await flow.host.advance("TASK", task)
    await flow.dispatch()
    await flow.host.advance("TASK", task)
    return task


async def plan(flow, task, racks, *, revision=1, **fields):
    data = dict(task_id=task, plan_revision=revision, **fields)
    if racks:
        data["added_bin_source_racks"] = [dict(rack_id=r, rack_face=["A"]) for r in racks]
    response = await flow.callback("sorting.task.plan_delta@v1", data)
    assert response.status_code == 202, response


async def rack_result(flow, action):
    cycle = next(
        r for r in await flow.rows("rack_cycle", roller=True) if r["id"] == action["cycle_id"]
    )
    response = await flow.callback(
        "transport.task.resulted@v1",
        dict(
            transport_task_id=action["transport_task_id"],
            kind="RACK_MOVE",
            rack_id=cycle["rack_id"],
            outcome_revision=1,
            status="SUCCEEDED",
            final_position=action["target"],
            arrival_face=action["target_face"],
        ),
    )
    assert response.status_code == 202, response
    await flow.host.advance("RACK_CYCLE", action["cycle_id"])


async def cycle_departure(flow, action, *, arrived=False):
    """Actual CTU01 Result, empty face decision and departure decision create CTU03."""
    flow.inbound_decision = {"result": "RACK_FACE_DONE"}
    flow.departure_decision = dict(
        result="READY", rack_destination=dict(type="ZONE", location_code="AT1-LOCAL-RETURN")
    )
    if not arrived:
        await rack_result(flow, action)
    for _ in range(6):
        await flow.dispatch()
        await flow.host.scan_pending()
    departures = [
        r
        for r in await flow.rows("rack_action", roller=True)
        if r["cycle_id"] == action["cycle_id"] and r["kind"] == "CTU03"
    ]
    assert len(departures) == 1
    await flow.dispatch()
    await flow.host.advance("RACK_CYCLE", action["cycle_id"])
    return departures[0]


async def saved_transports(flow):
    async with flow.factory() as session:
        return [
            dict(r) for r in (await session.execute(select(TransportTask.__table__))).mappings()
        ]


@pytest.mark.parametrize("line", [0, 1], ids=["KT16", "KT18"])
@pytest.mark.parametrize("role", ["RETURN", "TRANSFER"])
@pytest.mark.parametrize("same_source", [True, False])
async def test_accepted_manual_departure_keeps_closure_after_late_position(
    profile_flow, line, role, same_source
):
    flow = profile_flow
    tasks = [await ready(flow, line=i) for i in (0, 1)]
    flow.confirmation = dict(result="COMPLETED")
    flow.departure_decision = dict(
        result="READY", rack_destination=dict(type="ZONE", location_code="PROFILE-STORAGE")
    )
    for i, task in enumerate(tasks):
        rack = f"{flow.prefix}-{i}-{role}"
        fields = (
            dict(target_rack=dict(rack_id=rack, rack_face="A"))
            if role == "TRANSFER"
            else dict(
                added_direct_picks=[
                    dict(
                        source_locator=dict(
                            type="RACK_SLOT", rack_id=rack, rack_face="A", slot_id="S1"
                        )
                    )
                ]
            )
        )
        await plan(flow, task, [], **fields)
        await flow.host.advance("TASK", task)
    works = await flow.rows("rack")
    assert len(works) == 2
    entries = await flow.rows("rack_action")
    transports = await saved_transports(flow)
    for work in works:
        i = flow.lines.index(work["work_line_id"])
        action = next(a for a in entries if a["rack_work_id"] == work["id"])
        frozen = next(
            t for t in transports if t["transport_task_id"] == action["transport_task_id"]
        )["frozen"]
        assert frozen["rack_id"] == work["rack_id"]
        assert frozen["target"] == dict(
            kind="RACK_POSITION", location_code=flow.positions[i][role + "_RACK"]
        )
        assert (
            await flow.callback(
                "transport.task.resulted@v1",
                dict(
                    transport_task_id=action["transport_task_id"],
                    kind="RACK_MOVE",
                    rack_id=work["rack_id"],
                    outcome_revision=1,
                    status="SUCCEEDED",
                    final_position=frozen["target"],
                    arrival_face="A",
                ),
            )
        ).status_code == 202
        await flow.host.advance("MANUAL_RACK", work["id"])
        if role == "RETURN":
            assert (
                await flow.callback(
                    "outbound.return_rack.task_completed@v1",
                    dict(
                        task_id=work["task_id"],
                        rack_id=work["rack_id"],
                        through_plan_revision=1,
                        completed_at=1,
                    ),
                )
            ).status_code == 202
    for _ in range(3):
        await flow.dispatch()
        await flow.host.scan_pending()
    await asyncio.gather(*(flow.host.advance("MANUAL_RACK", w["id"]) for w in works))
    departures = [a for a in await flow.rows("rack_action") if a["kind"] == "DEPART"]
    assert len(departures) == 2
    own = next(w for w in works if w["work_line_id"] == flow.lines[line])
    for work in works:
        action = next(a for a in departures if a["rack_work_id"] == work["id"])
        final = (
            work["location_code"] if same_source and work["id"] == own["id"] else "PROFILE-STORE"
        )
        assert (
            await flow.callback(
                "transport.task.resulted@v1",
                dict(
                    transport_task_id=action["transport_task_id"],
                    kind="RACK_MOVE",
                    rack_id=work["rack_id"],
                    outcome_revision=1,
                    status="SUCCEEDED",
                    final_position=dict(kind="RACK_POSITION", location_code=final),
                ),
            )
        ).status_code == 202
    await asyncio.gather(*(flow.host.advance("MANUAL_RACK", own["id"]) for _ in range(2)))
    other = next(w for w in works if w["id"] != own["id"])
    assert await flow.host.advance("MANUAL_RACK", other["id"]) == "COMPLETED"
    assert await flow.host.advance("MANUAL_RACK", own["id"]) == "COMPLETED"
    assert len(await flow.rows("rack_action")) == 4


async def test_disabled_line_keeps_original_departure_and_resumes_selected_waiter(profile_flow):
    from app.businesses import catalog
    from app.models import AuditLog, ReliableMessage
    from app.schemas.workline import WorkLineUpdate
    from app.services.asset_audit import AssetActor
    from app.services.workline_service import update_workline

    flow = profile_flow
    actor = AssetActor(1, flow.prefix, None, None)

    async def configure(**changes):
        async with flow.factory() as session:
            line = await session.get(WorkLine, flow.lines[0])
            await update_workline(
                session,
                line.id,
                WorkLineUpdate(line_name=line.line_name, version=line.version, **changes),
                actor=actor,
                definition_lookup=catalog.get_definition,
            )

    try:
        await configure(
            positions=[
                {
                    "position_code": p["code"],
                    "capacity": 1 if p["role"] == "FIVE_RACK" else p["capacity"],
                }
                for p in __import__(
                    "tests.businesses.smt_manual.factory_profile", fromlist=["PROFILE"]
                ).PROFILE["lines"][0]["positions"]
                if p["role"] in ("FIVE_RACK", "RETURN_RACK", "TRANSFER_RACK")
            ]
        )
        tasks = [await ready(flow, line=i) for i in (0, 1)]
        await plan(flow, tasks[0], ["FIRST", "WAITER"])
        await flow.host.scan_pending()
        entry = (await flow.rows("rack_action", roller=True))[0]
        waiting = next(
            r
            for r in await flow.rows("face_service", roller=True)
            if r["cycle_id"] != entry["cycle_id"]
        )
        assert waiting["service_id"] and waiting["cycle_id"]
        # Freeze the original feed authorization while enabled; disable cannot grant new work.
        flow.inbound_decision = {"result": "RACK_FACE_DONE"}
        await rack_result(flow, entry)
        await flow.host.advance("RACK_CYCLE", entry["cycle_id"])
        assert (
            len([f for f in await flow.rows("face_work", roller=True) if f["feed_operation_id"]])
            == 1
        )
        await configure(is_enabled=False)
        await plan(flow, tasks[1], ["OTHER-LINE"])
        await cycle_departure(flow, entry, arrived=True)
        await flow.host.scan_pending()
        cycles = await flow.rows("rack_cycle", roller=True)
        assert next(c for c in cycles if c["id"] == entry["cycle_id"])["status"] == "RELEASED"
        actions = await flow.rows("rack_action", roller=True)
        assert any(a["kind"] == "CTU01" and a["work_line_id"] == flow.lines[1] for a in actions)
        assert not [a for a in actions if a["cycle_id"] == waiting["cycle_id"]]
        assert not [t for t in await saved_transports(flow) if t["cycle_id"] == waiting["cycle_id"]]

        await configure(is_enabled=True)
        for _ in range(2):
            await flow.host.scan_pending()
        own = [
            a
            for a in await flow.rows("rack_action", roller=True)
            if a["cycle_id"] == waiting["cycle_id"]
        ]
        assert len(own) == 1 and (own[0]["kind"], own[0]["source_ref"]) == (
            "CTU01",
            waiting["service_id"],
        )
        assert (
            len([t for t in await saved_transports(flow) if t["cycle_id"] == waiting["cycle_id"]])
            == 1
        )
        async with flow.factory() as session:
            assert (
                len(
                    (
                        await session.scalars(
                            select(ReliableMessage.id).where(
                                ReliableMessage.operation_id == own[0]["operation_id"]
                            )
                        )
                    ).all()
                )
                == 1
            )
    finally:
        async with flow.factory.begin() as session:
            await session.execute(
                AuditLog.__table__.delete().where(AuditLog.trace_id == flow.prefix)
            )


async def test_source_context_and_same_bin_route_to_own_scan2(profile_flow):
    flow = profile_flow
    assert flow.runtime.services["smt_manual"] is flow.host
    assert set(flow.host.components) == {"sorting_task", "bin_roller", "rack_dispatch"}
    assert flow.host.components["rack_dispatch"] is flow.roller.rack_dispatch
    async with flow.factory() as session:
        lines = list(await session.scalars(select(WorkLine).order_by(WorkLine.line_code)))
        assert [(r.line_code, r.version, r.is_enabled, r.business_code) for r in lines] == [
            ("KT16", 31, True, "smt_manual"),
            ("KT18", 7, True, "smt_manual"),
        ]
        assert len(list(await session.scalars(select(Device)))) == 12
        assert len(list(await session.scalars(select(DeviceRoute)))) == 12
        for line, source in zip(lines, PROFILE["lines"], strict=True):
            from app.models import WorkLinePosition

            assert (
                await session.scalar(
                    select(WorkLinePosition.capacity).where(
                        WorkLinePosition.work_line_id == line.id,
                        WorkLinePosition.position_code == line.position_bindings["FIVE_RACK"],
                    )
                )
                == 3
            )
            assert len(source["devices"]) == 6
            assert sum(d["has_contract"] for d in source["devices"]) == 4
            assert all(d["upstream_device_id"] is None for d in source["devices"])
            assert len(source["positions"]) == 5
            assert line.device_bindings == {
                d["role"]: d["device_code"] for d in source["devices"] if d["role"]
            }
            assert line.position_bindings == {p["role"]: p["code"] for p in source["positions"]}
            assert "SCAN2" not in line.position_bindings
            associated = set(
                await session.scalars(
                    select(Device.device_code).where(Device.work_line_id == line.id)
                )
            )
            assert associated == {d["device_code"] for d in source["devices"]}
    tasks = [await ready(flow, line=line) for line in (0, 1)]
    for line in (0, 1):
        assert (await flow.scan("SCAN1", "A000000099-B", line=line)).status_code == 200
        assert (await flow.scan("SCAN2", "A000000001-C", line=line)).status_code == 200
    await flow.dispatch()
    works = await flow.rows("manual_bin")
    for work in works:
        await flow.host.advance("MANUAL_BIN", work["arrival_id"])
    works = await flow.rows("manual_bin")
    assert {
        (r["work_line_id"], r["task_id"], r["device_code"], r["bin_code"], r["state"])
        for r in works
    } == {
        (flow.lines[i], tasks[i], flow.devices[i]["SCAN2"], "A000000001", "RELEASED")
        for i in (0, 1)
    }
    assert len({r["arrival_id"] for r in works}) == len({r["command_code"] for r in works}) == 2
    tasks_saved = await flow.rows("task")
    assert {(r["workline_code"], r["task_type"], r["business_type"]) for r in tasks_saved} == {
        ("KT16", "MANUAL", "outbound"),
        ("KT18", "MANUAL", "outbound"),
    }
    prepare = [
        body["data"]
        for _, body in flow.requests
        if body.get("operation") == "sorting.task.prepare@v1"
    ]
    assert {(r["workline_code"], r["task_id"]) for r in prepare} == set(
        zip(("KT16", "KT18"), tasks, strict=True)
    )
    async with flow.factory() as session:
        commands = list(await session.scalars(select(EcsCommand)))
        assert {r.device_code for r in commands} == {
            "STATION_SCAN9",
            "STATION_SCAN10",
            "STATION_SCAN13",
            "STATION_SCAN14",
        }
        assert all(json.loads(r.request_raw)["timeout"] == 30000 for r in commands)
        assert all((r.expires_at - r.created_at).total_seconds() == 300 for r in commands)
        assert all(json.loads(r.request_raw)["device_code"] == r.device_code for r in commands)
        assert all(r.route["origin"] == flow.origin for r in commands)


@pytest.mark.parametrize("line", [0, 1], ids=["KT16", "KT18"])
@pytest.mark.parametrize("completion", ["NORMAL", "NG"])
async def test_wait_due_work_required_completion_original_release(
    profile_flow, line, completion, monkeypatch
):
    flow = profile_flow
    now = datetime.now(UTC)
    monkeypatch.setattr(flow.host.capabilities.wms, "clock", lambda: now)
    task = await ready(flow, line=line)
    await plan(flow, task, [flow.prefix + "-source"])
    flow.admission["A000000001"] = dict(result="WAIT", retry_after_ms=200)
    await flow.scan("SCAN2", line=line)
    await flow.dispatch()
    work = (await flow.rows("manual_bin"))[0]
    assert await flow.host.advance("MANUAL_BIN", work["arrival_id"]) == "WAIT"
    original = next(r for r in await flow.rows("request") if r["kind"] == "BIN_ADMISSION")
    now += timedelta(milliseconds=199)
    assert await flow.host.advance("MANUAL_BIN", work["arrival_id"]) == "WAIT"
    assert len([r for r in await flow.rows("request") if r["kind"] == "BIN_ADMISSION"]) == 1
    flow.admission["A000000001"] = dict(result="WORK_REQUIRED", task_id=task)
    now += timedelta(milliseconds=1)
    assert await flow.host.advance("MANUAL_BIN", work["arrival_id"]) == "ADMISSION_PENDING"
    await flow.dispatch()
    assert await flow.host.advance("MANUAL_BIN", work["arrival_id"]) == "WORK_REQUIRED"
    requery = [r for r in await flow.rows("request") if r["kind"] == "BIN_ADMISSION"]
    assert len(requery) == 2
    assert {r["arrival_id"] for r in requery} == {work["arrival_id"]}
    assert len({r["operation_id"] for r in requery}) == 2
    assert any(r["operation_id"] == original["operation_id"] and r["response"] for r in requery)
    completed = dict(task_id=task, bin_code="A000000001", result=completion, completed_at=30)
    assert (await flow.callback("manual_bin.work_completed@v1", completed)).status_code == 202
    await flow.host.scan_pending()
    released = (await flow.rows("manual_bin"))[0]
    assert released["state"] == "RELEASED" and released["completed_result"] == completion
    assert released["command_code"]
    await flow.callback("manual_bin.work_completed@v1", completed)
    await flow.host.scan_pending()
    releases = [
        r
        for r in await flow.rows("scan_arrival", roller=True)
        if r["release_command_code"] is not None
    ]
    assert len(releases) == 1 and releases[0]["release_command_code"] == released["command_code"]
    async with flow.factory() as session:
        command = await session.get(EcsCommand, released["command_code"])
        assert command.device_code == flow.devices[line]["SCAN2"]
        assert command.applied_result_id is None
        assert json.loads(command.request_raw)["timeout"] == 30000


@pytest.mark.parametrize("line", [0, 1], ids=["KT16", "KT18"])
async def test_scan3_independent_scan4_own_success_fifo_outlet(profile_flow, line):
    flow = profile_flow
    # No task or SCAN2; return passage is independently accepted.
    for number in (1, 2):
        raw = f"A{number:09d}-B"
        await flow.scan("SCAN3", raw, line=line, timestamp=number * 10)
        await flow.scan("SCAN4", raw, line=line, timestamp=number * 10 + 1)
    await flow.dispatch()
    arrivals = await flow.rows("scan_arrival", roller=True)
    scans4 = sorted([r for r in arrivals if r["point"] == "SCAN4"], key=lambda r: r["scanned_at"])
    await flow.result(scans4[1]["command_code"])
    await flow.host.advance("RETURN", scans4[1]["id"])
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=flow.lines[line]) == ()
        assert (
            await flow.roller.get_return_candidates(session, work_line_id=flow.lines[1 - line])
            == ()
        )
    await flow.result(scans4[0]["command_code"])
    await flow.host.advance("RETURN", scans4[0]["id"])
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=flow.lines[line])
        assert [r["bin_code"] for r in candidates] == ["A000000001", "A000000002"]
    # Explicit READY return face is setup; public batch and Transport remain real product paths.
    batch_id = await manual_return_opportunity(flow, line=line)
    flow.return_decision = dict(
        result="READY",
        moves=[
            dict(
                sequence_no=i,
                bin_code=f"A{i:09d}",
                target=dict(
                    type="RACK_BIN_SLOT",
                    rack_id=flow.prefix + f"-return-{line}",
                    rack_face="A",
                    slot_id=f"S{i}",
                ),
            )
            for i in (1, 2)
        ],
    )
    await flow.dispatch()
    await flow.host.advance("FACE", batch_id)
    await flow.dispatch()
    batch = (await flow.rows("return_batch", roller=True))[0]
    transport = next(
        r
        for r in await saved_transports(flow)
        if r["transport_task_id"] == batch["transport_task_id"]
    )
    assert transport["workline_id"] == str(flow.lines[line])
    assert all(
        move["source"]
        == dict(kind="HANDOFF_POSITION", location_code=flow.positions[line]["OUTLET"])
        for move in transport["frozen"]["moves"]
    )
    assert [m["bin_code"] for m in batch["members"]] == ["A000000001", "A000000002"]


@pytest.mark.parametrize("line", [0, 1], ids=["KT16", "KT18"])
async def test_ctu01_inlet_and_distinct_return_transfer_positions(profile_flow, line):
    flow = profile_flow
    task = await ready(flow, line=line)
    rack = f"AT1-{PROFILE['lines'][line]['code']}-SOURCE"
    await plan(
        flow,
        task,
        [rack],
        target_rack=dict(rack_id=rack + "-TRANSFER", rack_face="A"),
        added_direct_picks=[
            dict(
                source_locator=dict(
                    type="RACK_SLOT", rack_id=rack + "-RETURN", rack_face="A", slot_id="S1"
                )
            )
        ],
    )
    await flow.host.scan_pending()
    await flow.dispatch()
    production = await flow.rows("rack")
    assert {(r["role"], r["location_code"], r["rack_id"]) for r in production} == {
        ("RETURN", flow.positions[line]["RETURN_RACK"], rack + "-RETURN"),
        ("TRANSFER", flow.positions[line]["TRANSFER_RACK"], rack + "-TRANSFER"),
    }
    transports = await saved_transports(flow)
    production_actions = await flow.rows("rack_action")
    production_transports = [
        next(r for r in transports if r["transport_task_id"] == action["transport_task_id"])
        for action in production_actions
    ]
    assert len(production_transports) == 2
    assert {
        (
            t["frozen"]["rack_id"],
            t["frozen"]["target"]["kind"],
            t["frozen"]["target"]["location_code"],
        )
        for t in production_transports
    } == {
        (rack + "-RETURN", "RACK_POSITION", flow.positions[line]["RETURN_RACK"]),
        (rack + "-TRANSFER", "RACK_POSITION", flow.positions[line]["TRANSFER_RACK"]),
    }
    action = next(r for r in await flow.rows("rack_action", roller=True) if r["kind"] == "CTU01")
    source_transport = next(
        r for r in transports if r["transport_task_id"] == action["transport_task_id"]
    )
    assert source_transport["frozen"]["target"] == dict(
        kind="RACK_POSITION", location_code=flow.positions[line]["FIVE_RACK"]
    )
    flow.inbound_decision = dict(
        result="READY",
        bins=[
            dict(
                bin_code="A000000003",
                source_locator=dict(
                    type="RACK_BIN_SLOT", rack_id=rack, rack_face="A", slot_id="S1"
                ),
            )
        ],
    )
    await rack_result(flow, action)
    await flow.dispatch()
    demand = next(
        r for r in await flow.rows("face_service", roller=True) if r["purpose"] == "SUPPLY"
    )
    await flow.host.advance("FACE", demand["id"])
    await flow.dispatch()
    segment = (await flow.rows("feed_segment", roller=True))[0]
    transport = next(
        r
        for r in await saved_transports(flow)
        if r["transport_task_id"] == segment["transport_task_id"]
    )
    assert transport["frozen"]["moves"][0]["target"] == dict(
        kind="HANDOFF_POSITION", location_code=flow.positions[line]["INLET"]
    )


@pytest.mark.parametrize("line", [0, 1], ids=["KT16", "KT18"])
async def test_ctu03_submit_acceptance_releases_original_cycle_without_final_result(
    profile_flow, line
):
    flow = profile_flow
    task = await ready(flow, line=line)
    await plan(flow, task, [f"AT1-{PROFILE['lines'][line]['code']}-RELEASE"])
    await flow.host.scan_pending()
    await flow.dispatch()
    action = (await flow.rows("rack_action", roller=True))[0]
    departure = await cycle_departure(flow, action)
    cycles = await flow.rows("rack_cycle", roller=True)
    assert cycles[0]["status"] == "RELEASED"
    assert cycles[0]["release_basis"]["response_code"] == "RECEIVED"
    async with flow.factory() as session:
        assert (
            await flow.roller.rack_dispatch.final_result(
                session, job_id=departure["cycle_id"], action_id=departure["id"]
            )
        ).outcome is None
    original = next(
        r
        for r in await saved_transports(flow)
        if r["transport_task_id"] == departure["transport_task_id"]
    )
    assert original["applied_outcome"] is None
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    actions = await flow.rows("rack_action", roller=True)
    assert len([r for r in actions if r["kind"] == "CTU03"]) == 1
    assert (await flow.rows("rack_cycle", roller=True))[0]["release_basis"] == cycles[0][
        "release_basis"
    ]


async def test_capacity_three_distinct_racks_repeated_wakeups_and_replenishment(profile_flow):
    flow = profile_flow
    tasks = [await ready(flow, line=line) for line in (0, 1)]
    racks = [
        [f"AT1-{PROFILE['lines'][line]['code']}-QUEUE-{n}" for n in range(1, 6)] for line in (0, 1)
    ]
    for line in (0, 1):
        await plan(flow, tasks[line], racks[line])
    # Default page size 100; unlimited accepting peer; no harness-side quota/concurrency cap.
    await flow.host.scan_pending()
    await flow.dispatch()
    first = [r for r in await flow.rows("rack_action", roller=True) if r["kind"] == "CTU01"]
    first_ids = {r["id"] for r in first}
    for _ in range(2):
        await flow.host.scan_pending()
        await flow.dispatch()
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    await flow.dispatch()
    repeated = [r for r in await flow.rows("rack_action", roller=True) if r["kind"] == "CTU01"]
    assert {r["id"] for r in repeated} == first_ids
    counts = {
        PROFILE["lines"][line]["code"]: len(
            [r for r in repeated if r["work_line_id"] == flow.lines[line]]
        )
        for line in (0, 1)
    }
    flow.observations = dict(initial_ctu01=counts, initial_action_ids=sorted(first_ids))
    selected = next(r for r in repeated if r["work_line_id"] == flow.lines[0])
    departure = await cycle_departure(flow, selected)
    # Existing waiters refill without a new plan publication.
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    await flow.dispatch()
    await flow.host.scan_pending()
    actions = await flow.rows("rack_action", roller=True)
    after = {
        PROFILE["lines"][line]["code"]: len(
            [r for r in actions if r["kind"] == "CTU01" and r["work_line_id"] == flow.lines[line]]
        )
        for line in (0, 1)
    }
    transports = await saved_transports(flow)
    submits = [
        body for _, body in flow.requests if body.get("operation") == "transport.task.submit@v1"
    ]
    flow.observations = dict(
        initial_ctu01=counts,
        after_one_KT16_release_ctu01=after,
        initial_action_ids=sorted(first_ids),
        registered_transport_count=len(transports),
        submitted_request_count=len(submits),
        ctu03_transport=departure["transport_task_id"],
        ctu03_has_final_outcome=next(
            t for t in transports if t["transport_task_id"] == departure["transport_task_id"]
        )["applied_outcome"]
        is not None,
        capacity_expected_each_line=3,
        peer="unlimited 202/RECEIVED; no test quota",
    )
    assert (counts, after) == ({"KT16": 3, "KT18": 3}, {"KT16": 4, "KT18": 3}), flow.observations
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    assert (
        len(
            [
                r
                for r in await flow.rows("rack_action", roller=True)
                if r["kind"] == "CTU01" and r["work_line_id"] == flow.lines[0]
            ]
        )
        == 4
    )
    # A new plan while full must retain the four original CTU01 identities.
    before_revision2 = {
        r["id"] for r in await flow.rows("rack_action", roller=True) if r["kind"] == "CTU01"
    }
    await plan(flow, tasks[0], ["AT1-KT16-QUEUE-6", "AT1-KT16-QUEUE-7"], revision=2)
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    await flow.dispatch()
    await flow.host.scan_pending()
    after_revision2 = [
        r for r in await flow.rows("rack_action", roller=True) if r["kind"] == "CTU01"
    ]
    assert {r["id"] for r in after_revision2} == before_revision2
    flow.observations["plan_revision2_added_racks"] = ["AT1-KT16-QUEUE-6", "AT1-KT16-QUEUE-7"]
    flow.observations["after_revision2_ctu01"] = {
        PROFILE["lines"][line]["code"]: sum(
            r["work_line_id"] == flow.lines[line] for r in after_revision2
        )
        for line in (0, 1)
    }
    assert flow.observations["after_revision2_ctu01"] == {"KT16": 4, "KT18": 3}
    second = next(
        r
        for r in repeated
        if r["work_line_id"] == flow.lines[0] and r["cycle_id"] != selected["cycle_id"]
    )
    second_departure = await cycle_departure(flow, second)
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    await flow.host.scan_pending()
    final_actions = await flow.rows("rack_action", roller=True)
    final = {
        PROFILE["lines"][line]["code"]: sum(
            a["kind"] == "CTU01" and a["work_line_id"] == flow.lines[line] for a in final_actions
        )
        for line in (0, 1)
    }
    flow.observations["after_second_KT16_release_ctu01"] = final
    flow.observations["second_ctu03_transport"] = second_departure["transport_task_id"]
    assert final == {"KT16": 5, "KT18": 3}
    assert (
        next(
            t
            for t in await saved_transports(flow)
            if t["transport_task_id"] == second_departure["transport_task_id"]
        )["applied_outcome"]
        is None
    )

    final_transports = await saved_transports(flow)
    assert (
        next(
            t for t in final_transports if t["transport_task_id"] == departure["transport_task_id"]
        )["applied_outcome"]
        is None
    )
    final_ids = {r["id"] for r in final_actions}
    await asyncio.gather(flow.host.scan_pending(), flow.host.scan_pending())
    await flow.dispatch()
    assert {r["id"] for r in await flow.rows("rack_action", roller=True)} == final_ids
    flow.observations["final_registered_transport_count"] = len(final_transports)
    flow.observations["final_submitted_request_count"] = sum(
        body.get("operation") == "transport.task.submit@v1" for _, body in flow.requests
    )


@pytest.mark.parametrize("destination_line", [0, 1], ids=["same-line", "KT18"])
async def test_known_cancelled_entry_blocks_only_its_conflict_destination(
    profile_flow, destination_line
):
    flow = profile_flow
    tasks = [await ready(flow, line=i) for i in (0, 1)]
    source = flow.prefix + "-cancelled"

    def direct(rack):
        return [
            dict(source_locator=dict(type="RACK_SLOT", rack_id=rack, rack_face="A", slot_id="S1"))
        ]

    await plan(
        flow,
        tasks[0],
        [],
        **(
            dict(target_rack=dict(rack_id=source, rack_face="A"))
            if destination_line == 0
            else dict(added_direct_picks=direct(source))
        ),
    )
    await flow.host.advance("TASK", tasks[0])
    work = (await flow.rows("rack"))[0]
    entry = (await flow.rows("rack_action"))[0]
    location = flow.positions[destination_line]["RETURN_RACK"]
    assert (
        await flow.callback(
            "transport.task.resulted@v1",
            dict(
                transport_task_id=entry["transport_task_id"],
                kind="RACK_MOVE",
                rack_id=source,
                outcome_revision=1,
                status="CANCELLED",
                final_position=dict(kind="RACK_POSITION", location_code=location),
            ),
        )
    ).status_code == 202
    assert await flow.host.advance("MANUAL_RACK", work["id"]) == "POSITION_CONFLICT"
    blocked, unrelated = flow.prefix + "-blocked", flow.prefix + "-unrelated"
    await plan(
        flow,
        tasks[destination_line],
        [],
        revision=2 if destination_line == 0 else 1,
        added_direct_picks=direct(blocked),
        **(
            dict(target_rack=dict(rack_id=unrelated, rack_face="A"))
            if destination_line == 1
            else {}
        ),
    )
    await flow.host.advance("TASK", tasks[destination_line])
    if destination_line == 0:
        await plan(flow, tasks[1], [], target_rack=dict(rack_id=unrelated, rack_face="A"))
        await flow.host.advance("TASK", tasks[1])
    works = await flow.rows("rack")
    assert not [r for r in works if r["rack_id"] == blocked]
    assert any(r["rack_id"] == unrelated for r in works)
    assert {a["rack_work_id"] for a in await flow.rows("rack_action")} == {r["id"] for r in works}


@pytest.mark.parametrize("stage", ["entry", "ready", "registered"])
async def test_manual_position_blocks_fresh_roller_work(profile_flow, stage):
    ready_face = stage != "entry"
    flow = profile_flow
    task = await ready(flow)
    rack, manual = flow.prefix + "-supply", flow.prefix + "-manual"
    await plan(
        flow, task, [rack] if ready_face else [], target_rack=dict(rack_id=manual, rack_face="A")
    )
    await flow.host.advance("TASK", task)
    if ready_face:
        await flow.host.scan_pending()
        (entry,) = await flow.rows("rack_action", roller=True)
        flow.inbound_decision = dict(
            result="READY",
            bins=[
                dict(
                    bin_code="A000000003",
                    source_locator=dict(
                        type="RACK_BIN_SLOT", rack_id=rack, rack_face="A", slot_id="S1"
                    ),
                )
            ],
        )
        await rack_result(flow, entry)
        await flow.dispatch()
        if stage == "registered":
            (face,) = await flow.rows("face_service", roller=True)
            await flow.host.advance("FACE", face["id"])
            (segment,) = await flow.rows("feed_segment", roller=True)
    (work,) = await flow.rows("rack")
    (manual_entry,) = await flow.rows("rack_action")
    assert (
        await flow.callback(
            "transport.task.resulted@v1",
            dict(
                transport_task_id=manual_entry["transport_task_id"],
                kind="RACK_MOVE",
                rack_id=manual,
                outcome_revision=1,
                status="CANCELLED",
                final_position=dict(
                    kind="RACK_POSITION", location_code=flow.positions[0]["FIVE_RACK"]
                ),
            ),
        )
    ).status_code == 202
    assert await flow.host.advance("MANUAL_RACK", work["id"]) == "POSITION_CONFLICT"
    if stage == "registered":
        assert (
            await flow.callback(
                "transport.task.resulted@v1",
                dict(
                    transport_task_id=segment["transport_task_id"],
                    kind="BIN_MOVE",
                    outcome_revision=1,
                    results=[
                        dict(
                            container_id="A000000003",
                            status="SUCCEEDED",
                            final_position={
                                "kind": "HANDOFF_POSITION",
                                "location_code": flow.positions[0]["INLET"],
                            },
                        )
                    ],
                ),
            )
        ).status_code == 202
        await flow.host.advance("FACE", face["id"])
        (consumed,) = await flow.rows("face_work", roller=True)
        assert consumed["feed_cursor"] == 1 and consumed["current_transport_task_id"] is None
    elif ready_face:
        (face,) = await flow.rows("face_service", roller=True)
        assert face["service_state"] == "READY"
        await flow.host.advance("FACE", face["id"])
        assert not [t for t in await saved_transports(flow) if t["kind"] == "BIN_MOVE"]
    else:
        await plan(flow, task, [rack], revision=2)
        await flow.host.scan_pending()
        assert not await flow.rows("rack_action", roller=True)
        other = await ready(flow, line=1)
        await plan(flow, other, [flow.prefix + "-other"])
        await flow.host.scan_pending()
        (allowed,) = await flow.rows("rack_action", roller=True)
        assert allowed["target"]["location_code"] == flow.positions[1]["FIVE_RACK"]
