"""Stage 1 exercises the public receivers and original outbox in isolated assembly."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.businesses.coarse_sorting import composition
from app.businesses.coarse_sorting.service import read_scan
from app.core.identifiers import uuid7
from app.integrations.ecs_command import CommandOwner
from app.models import Device, IntegrationExchange, WorkLine
from app.tasks.callback_tasks import run_history_cleanup
from app.tasks.reliable_tasks import run_cleanup
from tests.helpers import allocated, reach_allocate, reach_move
from tests.migration_helpers import replay_coarse_sorting_history

ALLOCATE = "inbound.coarse_sorting.storage_allocate@v1"
REPORT = "inbound.coarse_sorting.execution_report@v1"


async def dispatch_put(flow, row):
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 1
    return json.loads(flow.peer.commands[-1])


async def finish_put(flow, row, put, *, failed=False):
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result="FAILED" if failed else "SUCCESS",
            data={} if failed else {"actual_qty": 1, "location": "STATION_OUTPUT1"},
            error_detail={"code": "BLOCKED", "msg": "mock failure"} if failed else None,
        )
    ).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_REPORT_RESULT"


@pytest.mark.parametrize("bin_type", ["3", "2", "WMS-CUSTOM", 42])
async def test_original_bin_put_and_report_survive_ack_restart_and_rebind(direct, bin_type):
    flow = direct
    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, flow.line_ids[0])
        line.position_bindings = {**line.position_bindings, "PUT_COMPLETION": "STATION_OUTPUT1"}
    flow.output_locations = {flow.workline: "LEGACY_OUTPUT"}
    flow.rebuild()
    flow.peer.allocate = allocated(flow, bin_type=bin_type)
    row = await reach_allocate(flow)
    scan_id = row["id"]
    assert await flow.service.advance(scan_id) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 1
    put = json.loads(flow.peer.commands[-1])
    assert put["device_code"] == flow.output
    assert put["params"] == {
        "barcode": row["payload"]["barcode"],
        "bin_location": "B",
        "bin_cell_location": 7,
        "bin_type": str(bin_type),
        "reel_totalthickness": 120,
    }
    assert await flow.service.advance(scan_id) == "AWAITING_PUT_RESULT"
    assert not flow.peer.reports

    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.codes[0],
            data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
        )
    ).status_code == 404
    assert await flow.service.advance(scan_id) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 0

    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, flow.line_ids[0])
        line.version += 1
        line.is_enabled = False
        line.device_bindings = {"INPUT_ARM": flow.codes[0], "CONVEYOR": flow.conveyor_codes[0]}
        line.position_bindings = {**line.position_bindings, "PUT_COMPLETION": "STATION_OUTPUT2"}
    flow.rebuild()
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            data={"actual_qty": 1, "location": "STATION_OUTPUT1", "pick_and_put_result": "OK"},
            finish_time=1234,
        )
    ).status_code == 200
    assert await flow.service.advance(scan_id) == "AWAITING_REPORT_RESULT"
    async with flow.factory() as session:
        view = await read_scan(session, row["event_digest"])
    assert (view.get("put_result"), view.get("report_status")) == ("SUCCESS", "PENDING")
    flow.peer.report_code = "DUPLICATE"
    assert await flow.dispatch() == 1
    assert await flow.service.advance(scan_id) == "COMPLETED"
    async with flow.factory() as session:
        view = await read_scan(session, row["event_digest"])
    assert (view["put_result"], view["report_status"]) == ("SUCCESS", "COMPLETED")
    assert await flow.service.scan_pending() == 0
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 2
    assert len(flow.peer.reports) == 1
    assert json.loads(flow.peer.reports[0])["data"] == {
        "workline_code": flow.workline,
        "binding_id": "BIND-ORIGINAL",
        "bin_code": "BIN-A002",
        "bin_location": "B",
        "cell_id": "BIN-A002-1",
        "cell_index": 7,
        "completed_at": 1234,
        "result": "SUCCESS",
        "error_detail": None,
        "placement_status": "CONFIRMED_PLACED",
    }
    actions = await flow.action_rows(scan_id)
    assert {a["kind"] for a in actions} == {
        "VALIDATE_BIND",
        "MOVE_FORWARD",
        "ALLOCATE_STORAGE",
        "PUT_TO_BIN",
        "REPORT_COMPLETION",
    }


@pytest.mark.parametrize(
    "decision,reason",
    [
        ({"workline_code": "WRONG"}, "ALLOCATION_WORKLINE_MISMATCH"),
        ({"bin_location": "2"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"bin_location": "E"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"cell_index": 0}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"cell_index": 8}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"cell_index": "7"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"cell_index": True}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"cell_id": "BIN\x00A002-1"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
    ],
)
async def test_invalid_target_retains_original_allocation(direct, decision, reason):
    flow = direct
    flow.peer.allocate = allocated(flow, **decision)
    scan_id = (await reach_allocate(flow))["id"]
    for _ in range(2):
        assert await flow.service.advance(scan_id) == reason
        assert await flow.dispatch() == 0
    assert len(flow.peer.requests) == 2
    assert len(flow.peer.commands) == 1


@pytest.mark.parametrize("missing", ["cell_id", "cell_index"])
async def test_allocation_requires_both_storage_code_and_cell_index(direct, missing):
    direct.peer.allocate = allocated(direct)
    del direct.peer.allocate[missing]
    row = await reach_allocate(direct)
    assert await direct.service.advance(row["id"]) == "ALLOCATION_RECONCILIATION_REQUIRED"
    assert await direct.dispatch() == 0


async def test_wait_expiry_opens_one_round_for_original_binding(direct):
    flow = direct
    flow.peer.allocate = {"result": "WAIT", "workline_code": flow.workline}
    scan_id = (await reach_allocate(flow))["id"]
    assert await flow.service.advance(scan_id) == "NO_AVAILABLE_RACK"
    original = (await flow.rows())[0]["next_check_at"]
    assert original.replace(tzinfo=UTC) == flow.now + timedelta(seconds=5)
    flow.now += timedelta(milliseconds=4999)
    flow.rebuild()
    assert await flow.service.advance(scan_id) == "NO_AVAILABLE_RACK"
    assert (await flow.rows())[0]["next_check_at"] == original
    assert await flow.dispatch() == 0
    flow.now += timedelta(milliseconds=1)
    if flow.factory.kw["bind"].dialect.name == "postgresql":
        await asyncio.gather(*(flow.service.advance(scan_id) for _ in range(3)))
    else:
        for _ in range(3):
            await flow.service.advance(scan_id)
    assert await flow.dispatch() == 1
    first, second = map(json.loads, flow.peer.requests[1:])
    assert first["operation_id"] != second["operation_id"]
    assert (
        first["data"]
        == second["data"]
        == {
            "binding_id": "BIND-ORIGINAL",
            "workline_code": flow.workline,
        }
    )


async def test_scan_pending_skips_future_wait(direct, monkeypatch):
    flow = direct
    flow.peer.allocate = {"result": "WAIT", "workline_code": flow.workline}
    row = await reach_allocate(flow)
    assert await flow.service.advance(row["id"]) == "NO_AVAILABLE_RACK"
    called = []

    async def advance(service, identity, **kwargs):
        called.append(identity)

    monkeypatch.setattr(type(flow.service), "advance", advance)
    assert await flow.service.scan_pending() == 0
    assert called == []


async def test_unimplemented_departure_preserves_original_decision_without_requery(direct):
    from app.models import ReliableMessage

    flow = direct
    flow.peer.allocate = {
        "result": "DEPART_REQUIRED",
        "workline_code": flow.workline,
        "current_rack": {"rack_id": "RACK-1"},
    }
    scan_id = (await reach_allocate(flow))["id"]
    assert await flow.service.advance(scan_id) == "RACK_PREPARATION_STAGE_REQUIRED"
    original = (await flow.action_rows(scan_id))[-1]
    async with flow.factory.begin() as session:
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == original["message_id"])
        )
    flow.now += timedelta(hours=1)
    flow.rebuild()
    assert await flow.service.advance(scan_id) == "RACK_PREPARATION_STAGE_REQUIRED"
    assert await flow.dispatch() == 0
    for _ in range(2):
        assert await flow.service.advance(scan_id) == "RACK_PREPARATION_STAGE_REQUIRED"
    action = (await flow.action_rows(scan_id))[-1]
    assert action["evidence_history"] == original["evidence_history"]
    assert len(action["evidence_history"]) == 1
    assert action["evidence_history"][0]["data"] == flow.peer.allocate


async def test_wrong_station_preserves_original_put_for_reconciliation(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 1
    put = json.loads(flow.peer.commands[-1])
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            data={"actual_qty": 1, "location": "STATION_OUTPUT2"},
        )
    ).status_code == 200
    flow.rebuild()
    assert await flow.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
    assert await flow.dispatch() == 0
    assert (await flow.rows())[0]["state"] == "AWAITING_PUT"
    assert (await flow.action_rows(row["id"]))[-1]["command_code"] == put["command_code"]


@pytest.mark.parametrize(
    ("index", "proof"),
    [
        (None, "live"),
        (2, "live"),
        (None, "saved"),
        (2, "saved"),
        (2, "wrong-digest"),
        (2, "one-live"),
    ],
    ids=[
        "missing",
        "differs-from-command",
        "saved-failure",
        "saved-success",
        "wrong-digest",
        "one-live",
    ],
)
async def test_retained_target_must_match_frozen_put_before_reporting(
    direct, monkeypatch, index, proof
):
    direct.peer.allocate = allocated(direct)
    row = await reach_allocate(direct)
    put = await dispatch_put(direct, row)
    allocation, original = (await direct.action_rows(row["id"]))[-2:]
    evidence = allocation["evidence_history"][-1]
    target = dict(evidence["data"])
    if index is None:
        target.pop("cell_index")
    else:
        target["cell_index"] = index
    async with direct.factory.begin() as session:
        await session.execute(
            direct.actions.update()
            .where(direct.actions.c.id == allocation["id"])
            .values(evidence_history=[{**evidence, "data": target}])
        )

    outcome = "FAILED" if index is None else "SUCCESS"
    assert (
        await direct.result(
            row,
            command_code=put["command_code"],
            device_code=direct.output,
            result=outcome,
            data={} if outcome == "FAILED" else {"actual_qty": 1, "location": "STATION_OUTPUT1"},
            error_detail={"code": "1005", "msg": "Cancelled by operator"}
            if outcome == "FAILED"
            else None,
        )
    ).status_code == 200
    direct.rebuild()
    assert await direct.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
    assert await direct.dispatch() == 0
    assert direct.peer.reports == []
    current = (await direct.rows())[0]
    assert (current["state"], current["put_result"], current["report_status"]) == (
        "AWAITING_REPORT",
        outcome,
        "PENDING",
    )
    assert current["placed_at_ms"] == (None if outcome == "FAILED" else 1)
    actions = await direct.action_rows(row["id"])
    assert actions[-1]["id"] == original["id"]
    assert actions[-1]["command_code"] == put["command_code"]
    assert actions[-1]["evidence_history"][-1]["data"]["result"] == outcome

    from app.models import ReliableMessage
    from tests.helpers import clear_integration_history

    async with direct.factory.begin() as session:
        if proof in ("saved", "wrong-digest"):
            root_id = await session.scalar(
                select(IntegrationExchange.id).where(
                    IntegrationExchange.role == "ECS_COMMAND",
                    IntegrationExchange.command_code == put["command_code"],
                )
            )
            assert root_id is not None
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.exchange_id == root_id)
            )
            await clear_integration_history(session, (root_id,))
        if proof == "wrong-digest":
            saved = actions[-1]["evidence_history"][-1]
            await session.execute(
                direct.actions.update()
                .where(direct.actions.c.id == original["id"])
                .values(evidence_history=[{**saved, "digest": "invalid-digest"}])
            )
        await session.execute(
            direct.actions.update()
            .where(direct.actions.c.id == allocation["id"])
            .values(evidence_history=[evidence])
        )
    direct.rebuild()
    if proof == "one-live":

        async def missing_call(_session, **_identity):
            return None

        monkeypatch.setattr(direct.service.capabilities.ecs, "get_by_call", missing_call)
    if proof in ("wrong-digest", "one-live"):
        assert await direct.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
        assert await direct.dispatch() == 0
        assert len(direct.peer.commands) == 2
        assert direct.peer.reports == []
        assert (await direct.rows())[0] == current
        return
    assert await direct.service.advance(row["id"]) == "AWAITING_REPORT_RESULT"
    assert await direct.dispatch() == 1
    assert await direct.service.advance(row["id"]) == "COMPLETED"
    assert len(direct.peer.commands) == 2
    assert json.loads(direct.peer.reports[0])["data"]["result"] == outcome


async def test_incomplete_put_success_allows_later_valid_original_result(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    for data in (
        {},
        {"actual_qty": 1},
        {"location": "STATION_OUTPUT1"},
        {"actual_qty": 1, "location": "STATION_OUTPUT1", "pick_and_put_result": "PUT_FAILED"},
    ):
        assert (
            await flow.result(
                row, command_code=put["command_code"], device_code=flow.output, data=data
            )
        ).status_code == 400
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    await finish_put(flow, row, put)


async def test_put_zero_quantity_requires_explicit_nonplacement(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    for result, data in (
        ("SUCCESS", {"actual_qty": 0, "location": "STATION_OUTPUT1"}),
        ("FAILED", {"actual_qty": 0}),
        ("FAILED", {"actual_qty": 0, "pick_and_put_result": "PUT_FINISHED"}),
        ("FAILED", {"actual_qty": False, "pick_and_put_result": "PUT_FAILED"}),
        ("FAILED", {"actual_qty": 0, "pick_and_put_result": "PUT_FAILED", "location": "B"}),
    ):
        response = await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result=result,
            data=data,
            error_detail={"code": "BLOCKED", "msg": "mock failure"} if result == "FAILED" else None,
        )
        assert response.status_code == 400
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert not (await flow.action_rows(row["id"]))[-1]["evidence_history"]
    assert not flow.peer.reports


@pytest.mark.parametrize(
    ("failure_data", "error_detail", "finish_time"),
    [
        ({}, {"code": "BLOCKED", "msg": "mock failure"}, 1234),
        (
            {"actual_qty": 0, "pick_and_put_result": "PUT_FAILED"},
            {"code": "1005", "msg": "Cancelled by operator"},
            1790000060000,
        ),
    ],
    ids=["empty-data", "ecs-zero-quantity"],
)
async def test_confirmed_put_failure_closes_after_original_report_ack(
    direct, failure_data, error_detail, finish_time
):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result="FAILED",
            data=failure_data,
            error_detail=error_detail,
            finish_time=finish_time,
        )
    ).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_REPORT_RESULT"
    awaiting = (await flow.rows())[0]
    async with flow.factory() as session:
        view = await read_scan(session, row["event_digest"])
    assert (view.get("put_result"), view.get("report_status")) == ("FAILED", "PENDING")
    assert awaiting["state"] == "AWAITING_REPORT"
    assert awaiting["pending"]
    assert awaiting["completed_at"] is None
    assert awaiting["placed_at_ms"] is None
    assert not flow.peer.reports
    assert await flow.dispatch() == 1
    assert json.loads(flow.peer.reports[0])["data"] == {
        "workline_code": flow.workline,
        "binding_id": "BIND-ORIGINAL",
        "bin_code": "BIN-A002",
        "bin_location": "B",
        "cell_id": "BIN-A002-1",
        "cell_index": 7,
        "completed_at": finish_time,
        "result": "FAILED",
        "error_detail": error_detail,
        "placement_status": "CONFIRMED_NOT_PLACED",
    }

    flow.rebuild()
    async with flow.factory() as session:
        view = await read_scan(session, row["event_digest"])
    assert (view["put_result"], view["report_status"]) == ("FAILED", "PENDING")
    assert await flow.service.advance(row["id"]) == "COMPLETED"
    closed = (await flow.rows())[0]
    assert closed["state"] == "COMPLETED"
    async with flow.factory() as session:
        view = await read_scan(session, row["event_digest"])
    assert (view["put_result"], view["report_status"]) == ("FAILED", "COMPLETED")
    assert not closed["pending"]
    assert closed["completed_at"].replace(tzinfo=UTC) == flow.now
    assert closed["placed_at_ms"] is None
    assert closed["current_action_id"] == awaiting["current_action_id"]
    assert closed["allocation_action_id"] == awaiting["allocation_action_id"]
    assert await flow.service.scan_pending() == 0
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 2
    assert len(flow.peer.reports) == 1

    flow.peer.decision = {"result": "OK", "binding_id": "BIND-FOLLOWING"}
    following = await reach_allocate(
        flow, timestamp=1793206799001, changes={"barcode": "FOLLOWING"}
    )
    following_put = await dispatch_put(flow, following)
    assert following_put["command_code"] != put["command_code"]
    assert following_put["params"]["barcode"] == "FOLLOWING"
    await finish_put(flow, following, following_put)
    assert await flow.dispatch() == 1
    assert await flow.service.advance(following["id"]) == "COMPLETED"
    assert json.loads(flow.peer.reports[-1])["data"]["binding_id"] == "BIND-FOLLOWING"
    assert [
        (job["state"], job["put_result"], job["report_status"], job["placed_at_ms"])
        for job in await flow.rows()
    ] == [
        ("COMPLETED", "FAILED", "COMPLETED", None),
        ("COMPLETED", "SUCCESS", "COMPLETED", 1),
    ]


async def test_original_failed_put_report_closes_after_mock_round_activation(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    await finish_put(flow, row, put, failed=True)
    assert await flow.dispatch() == 1
    report = json.loads(flow.peer.reports[0])

    flow.rebuild()
    service = replace(flow.service, mock_rounds={flow.workline: "new-round"})
    assert await service.advance(row["id"]) == "COMPLETED"
    closed = (await flow.rows())[0]
    assert (closed["put_result"], closed["report_status"]) == ("FAILED", "COMPLETED")
    assert not closed["pending"]
    assert closed["placed_at_ms"] is None
    assert (await flow.action_rows(row["id"]))[-1]["operation_id"] == report["operation_id"]
    assert await flow.dispatch() == 0
    assert len(flow.peer.reports) == 1


@pytest.mark.parametrize("data", [{"actual_qty": 1}, {"location": "STATION_OUTPUT1"}])
async def test_failed_put_keeps_original_pending_without_releasing_target(direct, data):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    flow.peer.command = "invalid"
    put = await dispatch_put(flow, row)
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result="FAILED",
            data=data,
            error_detail={"code": "BLOCKED", "msg": "mock failure"},
        )
    ).status_code == 200
    assert await flow.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
    actions = await flow.action_rows(row["id"])
    action = actions[-1]
    assert [entry["kind"] for entry in actions] == [
        "VALIDATE_BIND",
        "MOVE_FORWARD",
        "ALLOCATE_STORAGE",
        "PUT_TO_BIN",
    ]
    assert action["command_code"] == put["command_code"]
    assert action["evidence_history"][0]["data"]["data"] == data
    held = (await flow.rows())[0]
    assert held["current_action_id"] == action["id"]
    assert held["state"] == "AWAITING_PUT"
    assert held["pending"]
    assert held["put_result"] is held["report_status"] is None
    flow.rebuild()
    for _ in range(2):
        assert await flow.service.scan_pending() == 1
        retained = (await flow.rows())[0]
        assert retained["pending"]
        assert retained["current_action_id"] == held["current_action_id"]
        assert retained["allocation_action_id"] == held["allocation_action_id"]
        assert retained["binding_id"] == held["binding_id"]
        assert retained["completed_at"] is None
    assert await flow.action_rows(row["id"]) == actions
    async with flow.factory() as session:
        command = await flow.commands.get_by_call(
            session, owner=CommandOwner("coarse_sorting"), call_key=action["call_key"]
        )
    assert (command.status, command.delivery_status) == ("FAILED", "DONE")
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 2
    assert flow.peer.reports == []


@pytest.mark.parametrize(
    ("outcome", "cell_id", "ack", "expected_reason"),
    [
        ("SUCCESS", "BIN-A002-1", "RECORDED", "COMPLETED"),
        ("SUCCESS", "BIN-A002-1", "DUPLICATE", "COMPLETED"),
        ("SUCCESS", "OTHER-CELL", "RECORDED", "PUT_RECONCILIATION_REQUIRED"),
        ("FAILED", "BIN-A002-1", "RECORDED", "PUT_RECONCILIATION_REQUIRED"),
    ],
    ids=["recorded", "duplicate", "wrong-target", "failed-put"],
)
async def test_legacy_success_report_ack_requires_matching_original_put(
    direct, monkeypatch, outcome, cell_id, ack, expected_reason
):
    from typing import Annotated

    from pydantic import Field

    from app.businesses.coarse_sorting.contracts import BinLocation, CellIndex, Reference
    from app.integrations.wms_request import RequestPayload, operation

    class LegacyExecutionReport(RequestPayload):
        workline_code: Reference
        binding_id: Reference
        bin_code: Reference
        bin_location: BinLocation
        cell_id: Reference
        cell_index: CellIndex
        completed_at: Annotated[int, Field(ge=0, le=2**63 - 1)]

    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    failed = outcome == "FAILED"
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result=outcome,
            data={} if failed else {"actual_qty": 1, "location": "STATION_OUTPUT1"},
            error_detail={"code": "BLOCKED", "msg": "mock failure"} if failed else None,
            finish_time=1234,
        )
    ).status_code == 200
    original = (await flow.rows())[0]
    operation_id, action_id = str(uuid7()), str(uuid7())
    frozen_data = {
        "workline_code": flow.workline,
        "binding_id": "BIND-ORIGINAL",
        "bin_code": "BIN-A002",
        "bin_location": "B",
        "cell_id": cell_id,
        "cell_index": 7,
        "completed_at": 1234,
    }
    # Freeze the old request with the original contract, then restart with the current registry.
    with monkeypatch.context() as legacy:
        registry = dict(operation.operations())
        registry[REPORT] = replace(registry[REPORT], data_model=LegacyExecutionReport)
        legacy.setattr(operation, "_registry", registry)
        async with flow.factory.begin() as session:
            reference = await flow.service.capabilities.wms.send(
                session, LegacyExecutionReport(**frozen_data), operation_id=operation_id
            )
            await session.execute(
                flow.actions.insert().values(
                    id=action_id,
                    job_id=row["id"],
                    kind="REPORT_COMPLETION",
                    round_no=1,
                    predecessor_action_id=original["current_action_id"],
                    request_data=frozen_data,
                    evidence_history=[],
                    created_at=flow.now,
                    operation=REPORT,
                    operation_id=operation_id,
                    message_id=reference.message_id,
                )
            )
            await session.execute(
                flow.table.update()
                .where(flow.table.c.id == row["id"])
                .values(
                    state="AWAITING_REPORT",
                    current_action_id=action_id,
                    put_result=outcome,
                    report_status="PENDING",
                    placed_at_ms=None if failed else 1234,
                    wait_reason="AWAITING_REPORT_RESULT",
                )
            )
    flow.rebuild()
    flow.peer.report_code = ack
    assert await flow.dispatch() == 1
    (original_wire,) = flow.peer.reports
    wire = json.loads(original_wire)
    assert (wire["operation"], wire["operation_id"], wire["data"]) == (
        REPORT,
        operation_id,
        frozen_data,
    )

    assert await flow.service.advance(row["id"]) == expected_reason
    current = (await flow.rows())[0]
    closed = expected_reason == "COMPLETED"
    assert (current["state"], current["put_result"], current["report_status"]) == (
        "COMPLETED" if closed else "AWAITING_REPORT",
        outcome,
        "COMPLETED" if closed else "PENDING",
    )
    assert current["pending"] is not closed
    assert current["current_action_id"] == action_id
    assert current["allocation_action_id"] == original["allocation_action_id"]
    report = (await flow.action_rows(row["id"]))[-1]
    assert (report["message_id"], report["operation_id"], report["request_data"]) == (
        reference.message_id,
        operation_id,
        frozen_data,
    )
    assert await flow.dispatch() == 0
    assert flow.peer.reports == [original_wire]
    assert len(flow.peer.commands) == 2


@pytest.mark.parametrize(
    "mode", ["undelivered", "acked-pending", "acked-inactive", "acked-wrong-target"]
)
async def test_legacy_failed_report_requires_original_active_put_proof(direct, mode):
    flow = direct
    delivered = mode != "undelivered"
    pending = mode != "acked-inactive"
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    flow.peer.command = "invalid"
    put = await dispatch_put(flow, row)
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            result="FAILED",
            data={},
            error_detail={"code": "BLOCKED", "msg": "mock failure"},
            finish_time=1234,
        )
    ).status_code == 200

    original = (await flow.rows())[0]
    operation_id, action_id = str(uuid7()), str(uuid7())
    report = flow.service.direct_put_contracts.report(
        workline_code=flow.workline,
        binding_id="BIND-ORIGINAL",
        bin_code="BIN-A002",
        bin_location="B",
        cell_id="OTHER-CELL" if mode == "acked-wrong-target" else "BIN-A002-1",
        cell_index=7,
        completed_at=1234,
        result="FAILED",
        error_detail={"code": "BLOCKED", "msg": "mock failure"},
        placement_status="CONFIRMED_NOT_PLACED",
    )
    # Seed the persisted action written by older builds, including its original outbox.
    async with flow.factory.begin() as session:
        reference = await flow.service.capabilities.wms.send(
            session, report, operation_id=operation_id
        )
        await session.execute(
            flow.actions.insert().values(
                id=action_id,
                job_id=row["id"],
                kind="REPORT_COMPLETION",
                round_no=1,
                predecessor_action_id=original["current_action_id"],
                request_data=report.model_dump(),
                evidence_history=[],
                created_at=flow.now,
                operation=REPORT,
                operation_id=operation_id,
                message_id=reference.message_id,
            )
        )
        await session.execute(
            flow.table.update()
            .where(flow.table.c.id == row["id"])
            .values(
                current_action_id=action_id, wait_reason="AWAITING_REPORT_RESULT", pending=pending
            )
        )
    if delivered:
        assert await flow.dispatch() == 1
    flow.rebuild()
    assert await flow.service.scan_pending() == int(pending)
    current = (await flow.rows())[0]
    assert current["current_action_id"] == action_id
    assert current["allocation_action_id"] == original["allocation_action_id"]
    assert current["binding_id"] == original["binding_id"]
    assert current["placed_at_ms"] is None
    actions = await flow.action_rows(row["id"])
    assert len(actions) == 5
    assert len(flow.peer.commands) == 2
    assert len(flow.peer.reports) == int(delivered)

    if mode == "acked-pending":
        assert current["state"] == current["wait_reason"] == "COMPLETED"
        assert (current["put_result"], current["report_status"]) == ("FAILED", "COMPLETED")
        assert not current["pending"]
        assert current["completed_at"] is not None
        assert await flow.service.scan_pending() == 0
        return
    if pending:
        assert current["state"] == "AWAITING_REPORT"
        assert (current["put_result"], current["report_status"]) == ("FAILED", "PENDING")
    else:
        assert current["state"] == "AWAITING_PUT"
        assert current["put_result"] is current["report_status"] is None
    assert current["wait_reason"] == (
        "PUT_RECONCILIATION_REQUIRED" if mode == "acked-wrong-target" else "AWAITING_REPORT_RESULT"
    )
    assert current["pending"] is pending
    assert current["completed_at"] is None
    cleanup_time = datetime.now(UTC) + timedelta(days=91)
    await run_cleanup(flow.factory, 90, now=cleanup_time)
    await run_history_cleanup(flow.factory, 90, now=cleanup_time)

    wms = flow.service.capabilities.wms
    allocate_action = next(
        action for action in actions if action["id"] == original["allocation_action_id"]
    )
    put_action = next(action for action in actions if action["id"] == original["current_action_id"])
    async with flow.factory() as session:
        allocation = await wms.get_result(
            session,
            message_id=allocate_action["message_id"],
            operation=ALLOCATE,
            operation_id=allocate_action["operation_id"],
        )
        failed_report = await wms.get_result(
            session, message_id=reference.message_id, operation=REPORT, operation_id=operation_id
        )
        command = await flow.commands.get_by_call(
            session, owner=CommandOwner("coarse_sorting"), call_key=put_action["call_key"]
        )
    assert (allocation.availability, failed_report.availability) == ("FOUND", "FOUND")
    assert (allocation.target, allocation.response_code, allocation.response_data) == (
        "coarse-wms",
        "DECIDED",
        flow.peer.allocate,
    )
    assert failed_report.target == "coarse-facts"
    assert failed_report.response_code == ("RECORDED" if delivered else None)
    assert failed_report.delivery_terminal is delivered
    assert command is not None
    assert (command.command_code, command.status, command.delivery_status) == (
        put["command_code"],
        "FAILED",
        "DONE",
    )
    assert command.result["error_detail"] == {"code": "BLOCKED", "msg": "mock failure"}
    if not pending:
        assert await flow.service.scan_pending() == 0
    assert (await flow.rows())[0] == current
    assert await flow.action_rows(row["id"]) == actions
    if mode == "undelivered":
        assert await flow.dispatch() == 1
        assert await flow.service.advance(row["id"]) == "COMPLETED"


@pytest.mark.parametrize("scenario", ["complete", "missing_proof", "source_changed"])
async def test_default_stop_and_retained_original_move_after_migration(direct, scenario):
    from app.models import ReliableMessage
    from app.schemas.workline import WorkLineUpdate
    from app.services.asset_audit import AssetActor
    from app.services.workline_service import update_workline

    flow = direct
    flow.rebuild(enabled=False)
    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, flow.line_ids[0])
        line.device_bindings = {
            key: value for key, value in line.device_bindings.items() if key != "OUTPUT_ARM"
        }
    row = await reach_move(flow)
    assert await flow.dispatch() == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
    assert await flow.service.scan_pending() == 0
    engine = flow.factory.kw["bind"]
    if engine.dialect.name == "postgresql":
        await replay_coarse_sorting_history(engine)

    from tests.helpers import clear_integration_history

    async with flow.factory.begin() as session:
        root_id = await session.scalar(
            select(IntegrationExchange.id).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == row["command_code"],
            )
        )
        await session.execute(delete(ReliableMessage).where(ReliableMessage.exchange_id == root_id))
        await clear_integration_history(session, (root_id,))
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == row["validation_message_id"])
        )
        if scenario == "missing_proof":
            await session.execute(
                flow.table.update().where(flow.table.c.id == row["id"]).values(move_result=None)
            )
        if scenario == "source_changed":
            device = await session.get(Device, row["device_id"])
            device.version += 1
    async with flow.factory() as session:
        line = await session.get(WorkLine, flow.line_ids[0])
        configured = await update_workline(
            session,
            line.id,
            WorkLineUpdate(
                line_name=line.line_name,
                version=line.version,
                device_bindings={**line.device_bindings, "OUTPUT_ARM": flow.output},
                positions=[{"position_code": line.position_bindings["OUTPUT_RACK"]}],
            ),
            actor=AssetActor(1, "stage1", None, None),
            definition_lookup=lambda _: composition.DEFINITION,
        )
        await session.commit()
    assert configured.version == row["work_line_version"] + 1
    flow.peer.allocate = allocated(flow)
    flow.rebuild()
    expected = {
        "complete": "AWAITING_ALLOCATION_RESULT",
        "missing_proof": "MOVE_RECONCILIATION_REQUIRED",
        "source_changed": "ORIGINAL_BINDING_CHANGED",
    }[scenario]
    assert await flow.service.advance(row["id"]) == expected
    assert await flow.dispatch() == int(scenario == "complete")
    retained = (await flow.rows())[0]
    for key in (
        "id",
        "event_digest",
        "validation_operation_id",
        "validation_message_id",
        "command_code",
        "work_line_version",
        "binding_revision",
    ):
        assert retained[key] == row[key]
    if scenario == "complete":
        assert retained["rack_binding_version"] == configured.version
        assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert len(flow.peer.commands) == 1
    assert [json.loads(raw)["operation"] for raw in flow.peer.requests] == (
        ["inbound.coarse_sorting.validate_bind@v1", ALLOCATE]
        if scenario == "complete"
        else ["inbound.coarse_sorting.validate_bind@v1"]
    )


async def test_full_station_keeps_the_second_same_barcode_on_original_move(direct):
    flow = direct
    now = datetime.now(UTC)
    flow.outbox.clock = flow.store.clock = flow.sender.clock = lambda: now
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    first_put = await dispatch_put(flow, first)

    flow.peer.decision = {"result": "OK", "binding_id": "BIND-SECOND"}
    second = await reach_move(flow, timestamp=1793206799001)
    assert first["id"] != second["id"]
    assert first["payload"]["barcode"] == second["payload"]["barcode"]
    flow.peer.command = "full"
    assert await flow.dispatch() == 1
    full_move = flow.peer.commands[-1]
    assert await flow.service.advance(second["id"]) == "AWAITING_MOVE_RESULT"
    assert await flow.service.advance(first["id"]) == "AWAITING_PUT_RESULT"
    assert [json.loads(raw)["operation"] for raw in flow.peer.requests].count(ALLOCATE) == 1
    assert not flow.peer.reports

    # The mock peer releases capacity after confirming the first physical placement.
    await finish_put(flow, first, first_put)
    assert await flow.dispatch() == 1
    assert await flow.service.advance(first["id"]) == "COMPLETED"
    flow.peer.command = "ack"
    now += timedelta(seconds=2)
    assert await flow.dispatch() == 1
    assert flow.peer.commands[-1] == full_move
    assert (await flow.result(second)).status_code == 200
    assert await flow.service.advance(second["id"]) == "AWAITING_ALLOCATION_RESULT"


async def test_original_validation_survives_cleanup_until_consumed(direct):
    flow = direct
    assert (await flow.post(flow.event())).status_code == 200
    scan_id = (await flow.rows())[0]["id"]
    assert await flow.service.advance(scan_id) == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    awaiting = (await flow.rows())[0]
    request = json.loads(flow.peer.requests[0])

    cleanup_time = datetime.now(UTC) + timedelta(days=91)
    await run_cleanup(flow.factory, 90, now=cleanup_time)
    await run_history_cleanup(flow.factory, 90, now=cleanup_time)
    async with flow.factory() as session:
        result = await flow.service.capabilities.wms.get_result(
            session,
            message_id=awaiting["validation_message_id"],
            operation=request["operation"],
            operation_id=request["operation_id"],
        )
    assert (result.availability, result.response_code, result.response_data) == (
        "FOUND",
        "DECIDED",
        flow.peer.decision,
    )
    flow.rebuild()
    assert await flow.service.advance(scan_id) == "AWAITING_MOVE_RESULT"
    resumed = (await flow.rows())[0]
    assert resumed["validation_operation_id"] == request["operation_id"]
    assert resumed["validation_message_id"] == awaiting["validation_message_id"]
    assert await flow.dispatch() == 1
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


@pytest.mark.parametrize("failed", [False, True], ids=["placed", "not-placed"])
async def test_original_report_survives_cleanup_until_consumed(direct, failed):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    await finish_put(flow, row, put, failed=failed)
    flow.peer.report_code = "RECORDED"
    assert await flow.dispatch() == 1

    cleanup_time = datetime.now(UTC) + timedelta(days=91)
    await run_cleanup(flow.factory, 90, now=cleanup_time)

    assert await flow.service.advance(row["id"]) == "COMPLETED"
    closed = (await flow.rows())[0]
    assert closed["put_result"] == ("FAILED" if failed else "SUCCESS")
    assert closed["report_status"] == "COMPLETED"
    assert await flow.dispatch() == 0
    assert len(flow.peer.reports) == 1

    report = next(
        action
        for action in await flow.action_rows(row["id"])
        if action["kind"] == "REPORT_COMPLETION"
    )
    await run_cleanup(flow.factory, 90, now=cleanup_time)
    await run_history_cleanup(flow.factory, 90, now=cleanup_time)
    async with flow.factory() as session:
        result = await flow.service.capabilities.wms.get_result(
            session,
            message_id=report["message_id"],
            operation=REPORT,
            operation_id=report["operation_id"],
        )
    assert result.availability == "NOT_FOUND"


@pytest.mark.parametrize("failed", [False, True], ids=["placed", "not-placed"])
async def test_height_comes_from_wms_while_lost_report_recovers_same_bytes(direct, failed):
    flow = direct
    flow.sender.uniform = lambda _lower, upper: upper
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    first_put = await dispatch_put(flow, first)
    assert first_put["params"]["reel_totalthickness"] == 120
    await finish_put(flow, first, first_put, failed=failed)
    flow.peer.report_code = None
    assert await flow.dispatch() == 1
    assert await flow.service.advance(first["id"]) == "REPORT_RECONCILIATION_REQUIRED"
    original_report = (await flow.action_rows(first["id"]))[-1]

    # WMS supplies the next physical height while the original report response is lost.
    flow.peer.decision = {"result": "OK", "binding_id": "BIND-SECOND"}
    next_height = 120 if failed else 140.25
    flow.peer.allocate = allocated(flow, reel_totalthickness=next_height)
    second = await reach_allocate(flow, timestamp=1793206799001)
    second_put = await dispatch_put(flow, second)
    assert second_put["params"]["reel_totalthickness"] == next_height
    assert second_put["params"]["bin_cell_location"] == 7
    assert first_put["command_code"] != second_put["command_code"]
    await finish_put(flow, second, second_put)
    flow.peer.report_code = "RECORDED"
    assert await flow.dispatch() == 1
    assert await flow.service.advance(second["id"]) == "COMPLETED"

    flow.rebuild()
    assert await flow.service.advance(first["id"]) == "REPORT_RECONCILIATION_REQUIRED"
    current, following = await flow.rows()
    assert current["put_result"] == ("FAILED" if failed else "SUCCESS")
    assert current["report_status"] == "PENDING"
    assert (following["put_result"], following["report_status"]) == ("SUCCESS", "COMPLETED")
    flow.store.clock = flow.sender.clock = lambda: datetime.now(UTC) + timedelta(seconds=65)
    flow.peer.report_code = "DUPLICATE"
    assert await flow.dispatch() == 1
    assert flow.peer.reports[0] == flow.peer.reports[2]
    assert await flow.service.advance(first["id"]) == "COMPLETED"
    latest_report = (await flow.action_rows(first["id"]))[-1]
    assert latest_report["operation_id"] == original_report["operation_id"]
    assert len(flow.peer.commands) == 4


async def test_held_put_retains_original_target_until_late_result(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    flow.peer.command = "invalid"
    put = await dispatch_put(flow, row)
    original = (await flow.rows())[0]
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    flow.rebuild()
    flow.peer.command = "ack"
    flow.now += timedelta(hours=1)
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 0
    retained = (await flow.rows())[0]
    assert retained["current_action_id"] == original["current_action_id"]
    assert retained["allocation_action_id"] == original["allocation_action_id"]
    assert not flow.peer.reports

    await finish_put(flow, row, put)
    assert await flow.dispatch() == 1
    assert await flow.service.advance(row["id"]) == "COMPLETED"
    assert len(flow.peer.commands) == 2


async def test_allocation_response_unknown_keeps_original_identity(direct):
    flow = direct
    flow.peer.allocate = {"result": "WAIT", "workline_code": flow.workline}
    row = await reach_move(flow)
    assert await flow.dispatch() == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_RESULT"
    flow.peer.decision_code = "UNKNOWN"
    assert await flow.dispatch() == 1
    original_raw = flow.peer.requests[-1]
    assert await flow.service.advance(row["id"]) == "ALLOCATION_RECONCILIATION_REQUIRED"
    flow.now += timedelta(hours=1)
    flow.rebuild()
    assert await flow.service.advance(row["id"]) == "ALLOCATION_RECONCILIATION_REQUIRED"
    action = (await flow.action_rows(row["id"]))[-1]
    assert action["operation_id"] == json.loads(original_raw)["operation_id"]
    assert action["round_no"] == 1
    assert (await flow.rows())[0]["next_check_at"] is None

    flow.store.clock = flow.sender.clock = lambda: datetime.now(UTC) + timedelta(seconds=2)
    flow.peer.decision_code = "DECIDED"
    assert await flow.dispatch() == 1
    assert flow.peer.requests[-1] == original_raw
    assert await flow.service.advance(row["id"]) == "NO_AVAILABLE_RACK"


@pytest.mark.parametrize(
    "state,reason,expected",
    [
        ("AWAITING_VALIDATION", "AWAITING_VALIDATION_RESULT", "VALIDATION_RECONCILIATION_REQUIRED"),
        ("AWAITING_MOVE", "AWAITING_MOVE_RESULT", "MOVE_RECONCILIATION_REQUIRED"),
    ],
)
async def test_missing_historical_references_hold_when_original_already_started(
    direct, state, reason, expected
):
    flow = direct
    assert (await flow.post(flow.event())).status_code == 200
    scan_id = (await flow.rows())[0]["id"]
    async with flow.factory.begin() as session:
        await session.execute(
            flow.table.update()
            .where(flow.table.c.id == scan_id)
            .values(
                state=state,
                wait_reason=reason,
            )
        )
    assert await flow.service.advance(scan_id) == expected
    assert await flow.dispatch() == 0
    assert not await flow.action_rows(scan_id)


async def test_missing_move_reference_does_not_repeat_a_physically_possible_move(direct):
    from app.models import ReliableMessage

    flow = direct
    row = await reach_move(flow)
    assert await flow.dispatch() == 1
    validation, move = await flow.action_rows(row["id"])
    async with flow.factory.begin() as session:
        await session.execute(
            flow.table.update()
            .where(flow.table.c.id == row["id"])
            .values(
                state="AWAITING_VALIDATION",
                wait_reason="ORIGINAL_BINDING_CHANGED",
                command_code=None,
                current_action_id=validation["id"],
            )
        )
        await session.execute(delete(flow.actions).where(flow.actions.c.id == move["id"]))
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.operation_id == row["command_code"])
        )
        from tests.helpers import clear_integration_history

        root_id = await session.scalar(
            select(IntegrationExchange.id).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == row["command_code"],
            )
        )
        await clear_integration_history(session, (root_id,))
    for _ in range(2):
        assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 1
    assert [a["kind"] for a in await flow.action_rows(row["id"])] == ["VALIDATE_BIND"]


async def test_invalid_historical_barcode_preserves_allocated_without_put(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow, changes={"barcode": ""})
    for _ in range(2):
        assert await flow.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
        assert await flow.dispatch() == 0
    action = (await flow.action_rows(row["id"]))[-1]
    assert action["kind"] == "ALLOCATE_STORAGE"
    assert len(action["evidence_history"]) == 1
    assert action["evidence_history"][0]["data"] == flow.peer.allocate


async def test_allocated_does_not_authorize_an_output_device_changed_since_request(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    async with flow.factory.begin() as session:
        output = await session.scalar(select(Device).where(Device.device_code == flow.output))
        output.version += 1
    assert await flow.service.advance(row["id"]) == "ORIGINAL_OUTPUT_ARM_CHANGED"
    assert await flow.dispatch() == 0
    action = (await flow.action_rows(row["id"]))[-1]
    assert action["kind"] == "ALLOCATE_STORAGE"
    assert action["evidence_history"][0]["data"] == flow.peer.allocate


async def test_original_job_reference_finds_scan_decision_command_and_result(direct):
    from app.schemas.integration_history import ExchangeListParams
    from app.services.integration_history_service import list_exchanges

    flow = direct
    row = await reach_move(flow)
    await flow.dispatch()
    assert (await flow.result(row)).status_code == 200
    async with flow.factory() as session:
        page = await list_exchanges(
            session,
            ExchangeListParams(
                reference_kind="BUSINESS_TASK",
                reference_namespace="coarse_sorting",
                reference_value=row["id"],
            ),
        )
        assert len(page["items"]) == 4
        assert {item["role"] for item in page["items"]} == {"RECEIPT", "OUTGOING", "ECS_COMMAND"}
        assert {item["protocol"] for item in page["items"]} == {
            "ecs-event",
            "wms-decisions",
            "ecs-command",
            "ecs-result",
        }
