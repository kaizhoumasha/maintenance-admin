"""Stage 1 exercises the public receivers and original outbox in isolated assembly."""

import asyncio
import json
import runpy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.businesses.coarse_sorting import composition
from app.core.identifiers import uuid7
from app.models import Device, DeviceRoute, WorkLine, WorkLinePosition

ALLOCATE = "inbound.coarse_sorting.storage_allocate@v1"
REPORT = "inbound.coarse_sorting.execution_report@v1"
JOB_MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations/versions/2026-11-06_coarse_sorting_job.py"
)


@pytest.fixture
async def direct(coarse_forward, monkeypatch):
    from app.integrations.ecs_command.declaration import compile_commands
    from app.integrations.registration import registration_source
    from app.integrations.wms_request import operation
    from app.main import app
    from app.models import EcsCommand, EcsResultEvidence
    from app.services.device_routes import DeviceRoutes

    flow = coarse_forward
    flow.peer.allocate = {}
    output = f"OUTPUT_{uuid7().hex}"
    route_id = str(uuid7())
    async with flow.factory.begin() as session:
        session.add(
            DeviceRoute(
                id=route_id,
                device_code=output,
                ecs_origin=flow.outbox.targets["coarse-wms"].base_url,
            )
        )
        await session.flush()
        line = await session.get(WorkLine, flow.line_ids[0])
        session.add(
            Device(
                device_code=output,
                display_name=output,
                work_line_id=line.id,
                current_route_id=route_id,
            )
        )
        line.device_bindings = {**line.device_bindings, "OUTPUT_ARM": output}
        flow.workline = line.line_code
    flow.output = output
    flow.now = datetime.now(UTC)
    flow.output_locations = {flow.workline: "STATION_OUTPUT1"}
    old_rebuild = flow.rebuild

    from app.businesses.coarse_sorting.ecs_command import MoveForward, PutToBin
    from app.businesses.coarse_sorting.models import CoarseSortingAction, CoarseSortingJob
    from app.businesses.coarse_sorting.request.inbound_coarse_sorting_execution_report_v1 import (
        ExecutionReport,
    )
    from app.businesses.coarse_sorting.request.inbound_coarse_sorting_storage_allocate_v1 import (
        StorageAllocate,
    )
    from app.integrations.wms_request import WmsRequests

    registered = dict(operation.operations())
    for name, endpoint, model in (
        (ALLOCATE, operation.Endpoint.DECISIONS, StorageAllocate),
        (REPORT, operation.Endpoint.FACTS, ExecutionReport),
    ):
        registered[name] = operation.Registered(
            name, endpoint, model, None, None, registration_source(model)
        )
    monkeypatch.setattr(operation, "_registry", registered)
    bindings, annexes = compile_commands(MoveForward, PutToBin)
    flow.commands.annexes = annexes
    flow.commands.routes = DeviceRoutes(bindings, annexes=annexes)
    flow.table = CoarseSortingJob.__table__
    flow.actions = CoarseSortingAction.__table__

    async def rows():
        async with flow.factory() as session:
            return list(
                (
                    await session.execute(
                        select(flow.table)
                        .where(flow.table.c.work_line_id == flow.line_ids[0])
                        .order_by(flow.table.c.id)
                    )
                ).mappings()
            )

    async def actions(scan_id):
        async with flow.factory() as session:
            return list(
                (
                    await session.execute(
                        select(flow.actions)
                        .where(flow.actions.c.job_id == scan_id)
                        .order_by(flow.actions.c.created_at, flow.actions.c.id)
                    )
                ).mappings()
            )

    def rebuild(*, enabled=True):
        old_rebuild()
        capabilities = flow.service.capabilities
        original_consumer = capabilities.wms.contract_consumers
        wms = WmsRequests(
            flow.outbox,
            owner_domain="coarse_sorting",
            contract_consumers=lambda key: (
                frozenset({"coarse_sorting"})
                if key[1] in {ALLOCATE, REPORT}
                else original_consumer(key)
            ),
        )
        flow.service = (
            composition.build_direct_put_service(
                replace(capabilities, wms=wms),
                output_locations=flow.output_locations,
                clock=lambda: flow.now,
            )
            if enabled
            else composition.build_service(capabilities)
        )
        flow.runtime.services["coarse_sorting"] = flow.service
        flow.runtime.progressions["coarse_sorting"] = flow.service.scan_pending
        monkeypatch.setattr(app.state, "business_runtime", flow.runtime)

    flow.rows, flow.action_rows, flow.rebuild = rows, actions, rebuild
    rebuild()

    try:
        yield flow
    finally:
        async with flow.factory.begin() as session:
            jobs = select(flow.table.c.id).where(flow.table.c.work_line_id == flow.line_ids[0])
            await session.execute(
                flow.table.update()
                .where(flow.table.c.id.in_(jobs))
                .values(
                    current_action_id=None,
                    allocation_action_id=None,
                    state="AWAITING_VALIDATION",
                    completed_at=None,
                )
            )
            await session.execute(delete(flow.actions).where(flow.actions.c.job_id.in_(jobs)))
            await session.execute(delete(flow.table).where(flow.table.c.id.in_(jobs)))
            codes = select(EcsCommand.command_code).where(EcsCommand.device_code == output)
            await session.execute(
                delete(EcsResultEvidence).where(EcsResultEvidence.command_code.in_(codes))
            )
            await session.execute(delete(EcsCommand).where(EcsCommand.device_code == output))
            await session.execute(
                delete(WorkLinePosition).where(WorkLinePosition.work_line_id == flow.line_ids[0])
            )
            await session.execute(delete(Device).where(Device.device_code == output))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == route_id))


def allocated(flow, **changes):
    return {
        "result": "ALLOCATED",
        "workline_code": flow.workline,
        "rack_id": "RACK-1",
        "bin_code": "BIN-A002",
        "bin_location": "2",
        "cell_id": "BIN-A002-1",
        "bin_type": "3",
        "reel_totalthickness": 120,
        **changes,
    }


async def reach_move(flow, *, timestamp=1793206799000, changes=None):
    assert (
        await flow.post(
            flow.event(timestamp=timestamp, changes={"thickness_mm": 20, **(changes or {})})
        )
    ).status_code == 200
    row = (await flow.rows())[-1]
    assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 1
    assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
    return (await flow.rows())[-1]


async def reach_allocate(flow, *, timestamp=1793206799000, changes=None):
    row = await reach_move(flow, timestamp=timestamp, changes=changes)
    assert await flow.dispatch() == 1
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.scan_pending() >= 1
    assert await flow.dispatch() == 1, "applied original MOVE_FORWARD must enqueue storage_allocate"
    assert json.loads(flow.peer.requests[-1])["operation"] == ALLOCATE
    return row


async def dispatch_put(flow, row):
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 1
    return json.loads(flow.peer.commands[-1])


async def finish_put(flow, row, put):
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
        )
    ).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_REPORT_RESULT"


async def test_original_bin_put_and_report_survive_ack_restart_and_rebind(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    scan_id = row["id"]
    assert await flow.service.advance(scan_id) == "AWAITING_PUT_RESULT"
    assert await flow.dispatch() == 1
    put = json.loads(flow.peer.commands[-1])
    assert put["device_code"] == flow.output
    assert put["params"] == {
        "barcode": row["payload"]["barcode"],
        "bin_location": "2",
        "bin_cell_location": "1",
        "bin_type": "3",
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
    flow.output_locations = {flow.workline: "STATION_OUTPUT2"}
    flow.rebuild()
    assert (
        await flow.result(
            row,
            command_code=put["command_code"],
            device_code=flow.output,
            data={"actual_qty": 1, "location": "STATION_OUTPUT1"},
            finish_time=1234,
        )
    ).status_code == 200
    assert await flow.service.advance(scan_id) == "AWAITING_REPORT_RESULT"
    flow.peer.report_code = "DUPLICATE"
    assert await flow.dispatch() == 1
    assert await flow.service.advance(scan_id) == "COMPLETED"
    assert await flow.service.scan_pending() == 0
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 2
    assert len(flow.peer.reports) == 1
    assert json.loads(flow.peer.reports[0])["data"] == {
        "workline_code": flow.workline,
        "binding_id": "BIND-ORIGINAL",
        "bin_code": "BIN-A002",
        "bin_location": "2",
        "cell_id": "BIN-A002-1",
        "completed_at": 1234,
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
        ({"cell_id": "BIN-A002-4"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
        ({"bin_location": "5"}, "ALLOCATION_RECONCILIATION_REQUIRED"),
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


async def test_empty_put_success_allows_later_valid_original_result(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    row = await reach_allocate(flow)
    put = await dispatch_put(flow, row)
    assert (
        await flow.result(row, command_code=put["command_code"], device_code=flow.output, data={})
    ).status_code == 400
    assert await flow.service.advance(row["id"]) == "AWAITING_PUT_RESULT"
    await finish_put(flow, row, put)


@pytest.mark.parametrize("scenario", ["complete", "missing_proof", "source_changed"])
async def test_default_stop_and_retained_original_move_after_migration(direct, scenario):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.models import EcsCommand, EcsResultEvidence, ReliableMessage
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
        migration = runpy.run_path(str(JOB_MIGRATION))
        ng_migration = runpy.run_path(
            str(JOB_MIGRATION.with_name("2026-11-06_coarse_sorting_ng.py"))
        )

        def remigrate(connection):
            with Operations.context(MigrationContext.configure(connection)):
                ng_migration["downgrade"]()
                migration["downgrade"]()
                migration["upgrade"]()
                ng_migration["upgrade"]()

        async with engine.begin() as connection:
            await connection.run_sync(remigrate)
        await engine.dispose()

    async with flow.factory.begin() as session:
        await session.execute(
            delete(EcsResultEvidence).where(EcsResultEvidence.command_code == row["command_code"])
        )
        await session.execute(
            delete(EcsCommand).where(EcsCommand.command_code == row["command_code"])
        )
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
    flow.store.clock = flow.sender.clock = lambda: datetime.now(UTC) + timedelta(seconds=2)
    assert await flow.dispatch() == 1
    assert flow.peer.commands[-1] == full_move
    assert (await flow.result(second)).status_code == 200
    assert await flow.service.advance(second["id"]) == "AWAITING_ALLOCATION_RESULT"


async def test_height_comes_from_wms_while_lost_report_recovers_same_bytes(direct):
    flow = direct
    flow.peer.allocate = allocated(flow)
    first = await reach_allocate(flow)
    first_put = await dispatch_put(flow, first)
    assert first_put["params"]["reel_totalthickness"] == 120
    await finish_put(flow, first, first_put)
    flow.peer.report_code = None
    assert await flow.dispatch() == 1
    assert await flow.service.advance(first["id"]) == "REPORT_RECONCILIATION_REQUIRED"
    original_report = (await flow.action_rows(first["id"]))[-1]

    # Simulate WMS knowing the next physical height while the original response was lost.
    flow.peer.decision = {"result": "OK", "binding_id": "BIND-SECOND"}
    flow.peer.allocate = allocated(flow, reel_totalthickness=140)
    second = await reach_allocate(flow, timestamp=1793206799001)
    second_put = await dispatch_put(flow, second)
    assert second_put["params"]["reel_totalthickness"] == 140
    assert second_put["params"]["bin_cell_location"] == "1"
    assert first_put["command_code"] != second_put["command_code"]

    flow.rebuild()
    assert await flow.service.advance(first["id"]) == "REPORT_RECONCILIATION_REQUIRED"
    flow.store.clock = flow.sender.clock = lambda: datetime.now(UTC) + timedelta(seconds=65)
    flow.peer.report_code = "DUPLICATE"
    assert await flow.dispatch() == 1
    assert flow.peer.reports[0] == flow.peer.reports[1]
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
    assert await flow.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
    flow.rebuild()
    flow.peer.command = "ack"
    flow.now += timedelta(hours=1)
    assert await flow.service.advance(row["id"]) == "PUT_RECONCILIATION_REQUIRED"
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
    from app.models import EcsCommand, ReliableMessage

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
        await session.execute(
            delete(EcsCommand).where(EcsCommand.command_code == row["command_code"])
        )
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
