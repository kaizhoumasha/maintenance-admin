"""Exercise original NG decisions and results through isolated peer assembly."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.util import CommandError
from sqlalchemy import delete, select

from app.businesses.coarse_sorting import composition
from app.businesses.coarse_sorting.models import CoarseSortingAction, CoarseSortingJob
from app.models import EcsCommand, EcsResultEvidence, ReliableMessage, WorkLine

JOB = CoarseSortingJob.__table__
ACTION = CoarseSortingAction.__table__
NG_DECISION = {"result": "NG", "reason_code": "BARCODE_INVALID"}
NG_PLATFORM = "STATION_NG_PLATFORM1"
NG_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/versions/2026-11-06_coarse_sorting_ng.py"
)


@pytest.fixture
async def ng(coarse_forward):
    flow = coarse_forward
    flow.peer.decision = NG_DECISION
    async with flow.factory() as session:
        line = await session.get(WorkLine, flow.line_ids[0])
        flow.ng_locations = {line.line_code: NG_PLATFORM}

    def assemble(*, enabled=False):
        if enabled:
            from app.businesses.coarse_sorting.ecs_command import MoveForward, MoveToNg
            from app.integrations.ecs_command.declaration import compile_commands
            from app.services.device_routes import DeviceRoutes

            bindings, annexes = compile_commands(MoveForward, MoveToNg)
            flow.commands.annexes = annexes
            flow.commands.routes = DeviceRoutes(bindings, annexes=annexes)
        capabilities = flow.service.capabilities
        service = (
            composition.build_ng_service(capabilities, ng_locations=flow.ng_locations)
            if enabled
            else composition.build_service(capabilities)
        )
        flow.service = service
        flow.runtime.services["coarse_sorting"] = service
        flow.runtime.progressions["coarse_sorting"] = service.scan_pending

    async def rows():
        async with flow.factory() as session:
            return list(
                (
                    await session.execute(
                        select(JOB).where(JOB.c.work_line_id.in_(flow.line_ids)).order_by(JOB.c.id)
                    )
                ).mappings()
            )

    async def actions(job_id):
        async with flow.factory() as session:
            return list(
                (
                    await session.execute(
                        select(ACTION)
                        .where(ACTION.c.job_id == job_id)
                        .order_by(ACTION.c.created_at, ACTION.c.id)
                    )
                ).mappings()
            )

    flow.assemble, flow.rows, flow.action_rows = assemble, rows, actions
    assemble()
    try:
        yield flow
    finally:
        async with flow.factory.begin() as session:
            jobs = JOB.c.work_line_id.in_(flow.line_ids)
            await session.execute(
                JOB.update()
                .where(jobs)
                .values(current_action_id=None, state="AWAITING_VALIDATION", completed_at=None)
            )
            await session.execute(
                delete(ACTION).where(ACTION.c.job_id.in_(select(JOB.c.id).where(jobs)))
            )
            await session.execute(delete(JOB).where(jobs))
            codes = select(EcsCommand.command_code).where(EcsCommand.device_code.in_(flow.codes))
            await session.execute(
                delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
            )
            await session.execute(delete(EcsCommand).where(EcsCommand.device_code.in_(flow.codes)))


async def accept_and_validate(flow, *, timestamp=1793206799000):
    assert (await flow.post(flow.event(timestamp=timestamp))).status_code == 200
    row = (await flow.rows())[-1]
    assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    return (await flow.rows())[-1]


async def test_formal_pending_ng_keeps_original_reason_without_command(ng):
    row = await accept_and_validate(ng)
    assert await ng.service.advance(row["id"]) == "AWAITING_NG_STAGE"
    saved = (await ng.rows())[0]
    assert saved.get("ng_reason_code") == NG_DECISION["reason_code"]
    assert saved["current_action_id"] == row["current_action_id"]
    assert await ng.dispatch() == 0


async def dispatch_ng(flow):
    assert await flow.service.scan_pending() >= 1
    assert await flow.dispatch() == 1
    return json.loads(flow.peer.commands[-1])


async def ng_result(flow, wire, **changes):
    return await flow.result(
        wire | {"conveyor_device_code": wire["device_code"]},
        **({"data": {"actual_qty": 1, "location": NG_PLATFORM}} | changes),
    )


@pytest.mark.parametrize("prior", [None, "action", "snapshot"])
async def test_original_ng_closes_only_after_matching_applied_success(ng, prior):
    if prior is None:
        ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    if prior:
        assert await ng.service.advance(row["id"]) == "AWAITING_NG_STAGE"
        async with ng.factory.begin() as session:
            values = {"ng_reason_code": None}
            if prior == "action":
                values["validation_response"] = None
            else:
                await session.execute(
                    ACTION.update()
                    .where(ACTION.c.id == row["current_action_id"])
                    .values(evidence_history=[], last_consumed_at=None)
                )
            await session.execute(JOB.update().where(JOB.c.id == row["id"]).values(**values))
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.id == row["validation_message_id"])
            )
        ng.assemble(enabled=True)
    wire = await dispatch_ng(ng)
    assert wire["task_type"] == "MOVE_TO_NG"
    assert wire["device_code"] == row["device_code"]
    assert wire["params"] == {
        "barcode": row["payload"]["barcode"],
        "reason_code": NG_DECISION["reason_code"],
    }
    after_ack = (await ng.rows())[0]
    assert after_ack["state"] == "AWAITING_NG"
    assert await ng.service.advance(row["id"]) == "AWAITING_NG_RESULT"
    assert after_ack["ng_reason_code"] == NG_DECISION["reason_code"]
    assert after_ack["binding_id"] is None
    actions = await ng.action_rows(row["id"])
    assert [action["kind"] for action in actions] == ["VALIDATE_BIND", "MOVE_TO_NG"]
    assert actions[0]["operation_id"] == row["validation_operation_id"]
    assert actions[1]["predecessor_action_id"] == actions[0]["id"]
    assert actions[1]["command_code"] == wire["command_code"]
    assert actions[1]["resource_snapshot"]["completion_location"] == NG_PLATFORM

    async with ng.factory.begin() as session:
        line = await session.get(WorkLine, ng.line_ids[0])
        line.version += 1
        line.is_enabled = False
    ng.ng_locations = {row["workline_code"]: "NEW_PLATFORM"}
    ng.assemble(enabled=True)
    assert (await ng_result(ng, wire)).status_code == 200
    assert await ng.service.scan_pending() == 1
    done = (await ng.rows())[0]
    assert done["state"] == "NG_COMPLETED"
    assert done["completed_at"] is not None
    assert done["current_action_id"] == actions[1]["id"]
    assert done["ng_reason_code"] == NG_DECISION["reason_code"]
    assert done["pending"] is False
    assert (await ng_result(ng, wire)).status_code == 200
    assert await ng.service.scan_pending() == 0
    assert await ng.dispatch() == 0
    assert len(ng.peer.requests) == len(ng.peer.commands) == 1
    assert ng.peer.reports == []


@pytest.mark.parametrize(
    "data,code,status",
    [
        ({"reason_code": "INVALID_DATA"}, "REJECTED", 422),
        (NG_DECISION, "DECIDED", 502),
        ({"result": "NG"}, "DECIDED", 200),
        ({"result": "NG", "reason_code": "R", "binding_id": "B"}, "DECIDED", 200),
    ],
)
async def test_rejected_unavailable_or_invalid_validation_creates_no_ng(ng, data, code, status):
    ng.assemble(enabled=True)
    ng.peer.decision, ng.peer.decision_code, ng.peer.decision_status = data, code, status
    row = await accept_and_validate(ng)
    await ng.service.scan_pending()
    assert [action["kind"] for action in await ng.action_rows(row["id"])] == ["VALIDATE_BIND"]
    saved = (await ng.rows())[0]
    assert saved["state"] == "AWAITING_VALIDATION"
    assert saved["ng_reason_code"] is None
    assert await ng.dispatch() == 0
    assert ng.peer.commands == []


async def test_historical_ng_missing_original_evidence_is_retained(ng):
    row = await accept_and_validate(ng)
    assert await ng.service.advance(row["id"]) == "AWAITING_NG_STAGE"
    async with ng.factory.begin() as session:
        await session.execute(
            JOB.update()
            .where(JOB.c.id == row["id"])
            .values(ng_reason_code=None, validation_response=None)
        )
        await session.execute(
            ACTION.update()
            .where(ACTION.c.id == row["current_action_id"])
            .values(evidence_history=[], last_consumed_at=None)
        )
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == row["validation_message_id"])
        )
    ng.assemble(enabled=True)
    assert await ng.service.advance(row["id"]) == "NG_RECONCILIATION_REQUIRED"
    assert await ng.dispatch() == 0
    assert len(ng.peer.requests) == 1
    assert ng.peer.commands == []


async def test_ng_validation_retained_until_current_authorization_is_restored(ng):
    ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    async with ng.factory.begin() as session:
        line = await session.get(WorkLine, ng.line_ids[0])
        line.is_enabled = False
    assert await ng.service.advance(row["id"]) == "WORKLINE_DISABLED"
    blocked = (await ng.rows())[0]
    assert blocked["state"] == "AWAITING_VALIDATION"
    assert blocked["ng_reason_code"] == NG_DECISION["reason_code"]
    assert blocked["current_action_id"] == row["current_action_id"]
    assert await ng.dispatch() == 0
    async with ng.factory.begin() as session:
        line = await session.get(WorkLine, ng.line_ids[0])
        line.is_enabled = True
        await session.execute(
            delete(ReliableMessage).where(ReliableMessage.id == row["validation_message_id"])
        )
    wire = await dispatch_ng(ng)
    assert wire["device_code"] == row["device_code"]
    assert len(ng.peer.requests) == 1


async def test_missing_ng_platform_retains_original_decision_without_registering_move(ng):
    ng.ng_locations = {}
    ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    assert await ng.service.advance(row["id"]) == "NG_PLATFORM_CONFIGURATION_REQUIRED"
    saved = (await ng.rows())[0]
    assert saved["state"] == "AWAITING_VALIDATION"
    assert saved["ng_reason_code"] == NG_DECISION["reason_code"]
    assert saved["current_action_id"] == row["current_action_id"]
    assert await ng.dispatch() == 0


async def test_wrong_device_and_malformed_ng_result_cannot_close(ng):
    ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    wire = await dispatch_ng(ng)
    assert (await ng_result(ng, wire, device_code=ng.codes[1])).status_code == 404
    for data in (
        {"actual_qty": True, "location": NG_PLATFORM},
        {"actual_qty": 1, "location": NG_PLATFORM, "extra": "not in annex"},
        {"actual_qty": 1},
    ):
        assert (await ng_result(ng, wire, data=data)).status_code == 400
    assert await ng.service.advance(row["id"]) == "AWAITING_NG_RESULT"
    assert (await ng_result(ng, wire)).status_code == 200
    assert await ng.service.advance(row["id"]) == "NG_COMPLETED"


@pytest.mark.parametrize(
    "changes",
    [
        {"data": {"actual_qty": 1, "location": "WRONG_NG_PLATFORM"}},
        {"result": "FAILED", "data": {}, "error_detail": {"code": "BLOCKED", "msg": "mock"}},
    ],
)
async def test_applied_conflict_or_failure_keeps_original_ng_responsibility(ng, changes):
    ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    wire = await dispatch_ng(ng)
    assert (await ng_result(ng, wire, **changes)).status_code == 200
    assert await ng.service.advance(row["id"]) == "NG_RECONCILIATION_REQUIRED"
    held = (await ng.rows())[0]
    assert held["state"] == "AWAITING_NG"
    assert held["completed_at"] is None
    assert (await ng_result(ng, wire)).status_code == 409
    assert await ng.service.scan_pending() == 1
    assert (await ng.rows())[0]["current_action_id"] == held["current_action_id"]
    action = (await ng.action_rows(row["id"]))[-1]
    assert len(action["evidence_history"]) == 1
    assert action["command_code"] == wire["command_code"]
    assert await ng.dispatch() == 0
    assert len(ng.peer.commands) == 1
    assert ng.peer.reports == []


async def test_same_barcode_full_station_and_unknown_delivery_keep_separate_original_actions(ng):
    ng.assemble(enabled=True)
    first = await accept_and_validate(ng)
    ng.peer.command = "invalid"
    first_wire = await dispatch_ng(ng)
    assert await ng.service.advance(first["id"]) == "NG_RECONCILIATION_REQUIRED"
    second = await accept_and_validate(ng, timestamp=1793206799001)
    ng.peer.command = "full"
    second_wire = await dispatch_ng(ng)
    assert second_wire["params"] == first_wire["params"]
    assert second_wire["command_code"] != first_wire["command_code"]
    assert await ng.service.advance(second["id"]) == "AWAITING_NG_RESULT"
    assert await ng.service.scan_pending() == 2
    assert len(ng.peer.commands) == 2
    assert (await ng_result(ng, first_wire)).status_code == 200
    assert await ng.service.advance(first["id"]) == "NG_COMPLETED"
    first_now, second_now = await ng.rows()
    assert first_now["state"] == "NG_COMPLETED"
    assert second_now["state"] == "AWAITING_NG"
    assert (await ng.action_rows(second["id"]))[-1]["command_code"] == second_wire["command_code"]
    full_attempt = ng.peer.commands[-1]
    ng.peer.command = "ack"
    ng.store.clock = ng.sender.clock = lambda: datetime.now(UTC) + timedelta(seconds=2)
    assert await ng.dispatch() == 1
    assert ng.peer.commands[-1] == full_attempt
    assert (await ng_result(ng, second_wire)).status_code == 200
    assert await ng.service.advance(second["id"]) == "NG_COMPLETED"
    assert len(await ng.action_rows(second["id"])) == 2
    assert ng.peer.reports == []


async def test_ng_migration_refuses_to_drop_original_action_and_reason(ng):
    ng.assemble(enabled=True)
    row = await accept_and_validate(ng)
    wire = await dispatch_ng(ng)
    downgrade = runpy.run_path(str(NG_MIGRATION))["downgrade"]

    def remove_ng_schema(connection):
        with Operations.context(MigrationContext.configure(connection)):
            downgrade()

    with pytest.raises(CommandError, match="NG evidence exists"):
        async with ng.factory.kw["bind"].begin() as connection:
            await connection.run_sync(remove_ng_schema)
    assert (await ng.rows())[0]["ng_reason_code"] == NG_DECISION["reason_code"]
    assert (await ng.action_rows(row["id"]))[-1]["command_code"] == wire["command_code"]
