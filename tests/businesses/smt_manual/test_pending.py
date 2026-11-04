from unittest.mock import Mock

import pytest

from app.businesses._components.rack_dispatch.contracts import JobOrigin
from app.businesses.runtime import Capabilities
from app.businesses.smt_manual.service import KINDS, SmtManualService


@pytest.mark.parametrize("later_task_id", ["T9", "T15"])
async def test_pending_pages_cover_production_and_all_roller_kinds(
    manual_host, monkeypatch, later_task_id
):
    flow = manual_host
    for i in range(3):
        await flow.issue(f"T{i}")
    async with flow.factory.begin() as session:
        for i in range(3):
            identity = f"pending-{i}"
            scope = dict(id=identity, owner_domain="smt_manual", work_line_id=flow.lines[0])
            await flow.host.rack_dispatch.register(
                session,
                JobOrigin(
                    "ROLLER_ORDINARY",
                    identity,
                    flow.lines[0],
                    "R",
                    "T0",
                    None,
                    None,
                    "A",
                    None,
                    identity,
                    None,
                ),
            )
            for key, values in {
                "bin_passage": dict(
                    scope_kind="TASK",
                    scope_id="T0",
                    scan1_event_id=i + 1,
                    scan1_visit_key=identity,
                    scan1_command_code=identity,
                    scan2_event_id=i + 11,
                    scan2_visit_key="release-" + identity,
                    production_record_id=identity,
                    release_command_code="release-" + identity,
                ),
                "bin_return": dict(
                    scope_id="line-run",
                    bin_code=f"A{i:09}",
                    scan3_event_id=i + 21,
                    scan3_visit_key=identity,
                    scan3_command_code=identity,
                    return_state="WAITING",
                ),
                "face_work": dict(
                    scope_id="T0",
                    purpose="SUPPLY",
                    change_id=identity,
                    plan_revision=i,
                    rack_id="R",
                    rack_face="A",
                    demand_state="ACTIVE",
                    feed_cursor=0,
                ),
            }.items():
                await session.execute(flow.roller_tables[key].insert().values(**scope, **values))
            await session.execute(
                flow.roller_tables["workline_state"]
                .insert()
                .values(
                    owner_domain="smt_manual",
                    work_line_id=100 + i,
                    line_run_id=identity,
                    drain_phase="REQUESTED",
                    drain_operation_id=identity,
                    drain_message_id=i + 1,
                    candidate_cursor=0,
                )
            )
            await session.execute(
                flow.tables["manual_bin"]
                .insert()
                .values(
                    arrival_id=identity,
                    work_line_id=flow.lines[0],
                    device_code="SCAN2",
                    raw_bin_code="A000000001-C",
                    scanned_at=1,
                    task_id="T0",
                    state="WAIT",
                    pending=True,
                )
            )
            await session.execute(
                flow.tables["input"]
                .insert()
                .values(
                    operation="manual_bin.work_completed@v1",
                    operation_id=identity,
                    receipt_id=i,
                    work_line_id=flow.lines[0],
                    task_id="T0",
                    bin_code="A000000001",
                    completion_result="NORMAL",
                    completed_at=1,
                    timestamp=1,
                    payload={},
                    state="PENDING",
                )
            )
            job_id = await flow.host.rack_dispatch.register(
                session,
                JobOrigin(
                    "MANUAL_RETURN",
                    identity,
                    flow.lines[0],
                    "R",
                    "T0",
                    "SMT-M1-1",
                    "RETURN",
                    "A",
                    None,
                    "manual-" + identity,
                    None,
                ),
            )
            await session.execute(
                flow.tables["request"]
                .insert()
                .values(
                    kind="RACK_ARRIVAL_REPORT",
                    operation="outbound.return_rack.arrival_report@v1",
                    operation_id=identity,
                    task_id="T0",
                    work_line_id=flow.lines[0],
                    rack_work_id=job_id,
                    action_id=identity,
                    message_id=i + 1,
                )
            )
    visited = []

    async def waiting(kind, record_id):
        visited.append((kind, record_id))
        if len(visited) == 1:
            await flow.issue(later_task_id)
            raise RuntimeError("one original item failed")
        return "WAIT"

    monkeypatch.setattr(flow.host, "advance", waiting)
    assert await flow.host.scan_pending(limit=1) == len(KINDS) * 3
    assert [k for k, _ in visited[: len(KINDS)]] == list(KINDS)
    assert all(sum(k == kind for k, _ in visited) == 3 for kind in KINDS)
    assert [rid for kind, rid in visited if kind == "SCAN2_RELEASE"] == [
        "pending-0",
        "pending-1",
        "pending-2",
    ]
    assert ("TASK", later_task_id) not in visited
    visited.clear()
    assert await flow.host.scan_pending(limit=2) == len(KINDS) * 3 + 1
    assert ("TASK", later_task_id) in visited


async def test_terminal_task_stops_original_scheduling_pending(
    manual_host, manual_completion_tools, monkeypatch
):
    flow = manual_host
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.scan()
    await flow.decision(dict(result="NO_WORK"))
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    assert await flow.host.advance("MANUAL_BIN", arrival) == "RELEASED"
    await flow.host.advance("TASK", "T1")
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.host.advance("TASK", "T1") == "COMPLETED"
    release = (await flow.rows("scan2_release", component=True))[0]
    visited = []
    original = flow.host.advance

    async def record(kind, record_id):
        visited.append((kind, record_id))
        return await original(kind, record_id)

    monkeypatch.setattr(flow.host, "advance", record)
    await flow.host.scan_pending(limit=1)
    assert ("SCAN2_RELEASE", release["id"]) not in visited
    assert (await flow.rows("scan2_release", component=True))[0]["command_code"] == release[
        "command_code"
    ]


async def test_terminal_task_keeps_original_production_rack_pending(
    manual_host, manual_completion_tools
):
    flow = manual_host
    await flow.ready()
    await flow.plan(faces=(), target=dict(rack_id="TRANSFER", rack_face="B"))
    await flow.host.advance("TASK", "T1")
    rack = (await flow.rows("rack"))[0]
    await flow.rack_outcome(face="B")
    await flow.host.advance("MANUAL_RACK", rack["id"])
    await flow.host.advance("TASK", "T1")
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.host.advance("TASK", "T1") == "COMPLETED"
    assert await flow.host.scan_pending(limit=1) == 1
    original = (await flow.rows("rack_departure"))[0]
    assert original["rack_work_id"] == rack["id"]
    await flow.issue("T2")
    await flow.host.scan_pending(limit=1)
    assert [d["operation_id"] for d in await flow.rows("rack_departure")] == [
        original["operation_id"]
    ]
    assert (await flow.rows("rack"))[0]["task_id"] == "T1"


@pytest.mark.parametrize("limit", [None, True, False, 0, -1, 1.5, "1"])
async def test_invalid_sweep_limit_opens_no_transaction(limit):
    factory = Mock(side_effect=AssertionError("invalid input must not open a transaction"))
    service = SmtManualService(Capabilities(None, object(), object(), factory))
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        await service.scan_pending(limit=limit)
    factory.assert_not_called()


async def test_unknown_work_kind_opens_no_transaction():
    factory = Mock(side_effect=AssertionError("invalid input must not open a transaction"))
    service = SmtManualService(Capabilities(None, object(), object(), factory))
    with pytest.raises(ValueError, match="unsupported manual work kind"):
        await service.advance("UNSUPPORTED", "original")
    factory.assert_not_called()
