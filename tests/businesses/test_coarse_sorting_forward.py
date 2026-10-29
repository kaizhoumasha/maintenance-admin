"""Original scan, decision and movement evidence survive retries and process restarts."""

import json

import pytest


async def test_two_lines_complete_original_move_and_keep_raw_identity(coarse_forward):
    flow = coarse_forward
    for device in (0, 1):
        assert (await flow.post(flow.event(device=device))).status_code == 200
    for row in await flow.rows():
        assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    assert await flow.dispatch() == 2
    for row in await flow.rows():
        assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
    rows = await flow.rows()
    assert len({r["validation_operation_id"] for r in rows}) == 2
    assert len({r["command_code"] for r in rows}) == 2
    assert {r["conveyor_device_code"] for r in rows} == set(flow.conveyor_codes)
    assert await flow.dispatch() == 2
    for row in rows:
        assert await flow.service.advance(row["id"]) == "AWAITING_MOVE_RESULT"
        assert (await flow.result(row)).status_code == 200
        assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
    retained = await flow.rows()
    flow.rebuild()
    for row in retained:
        assert row["pending"] is True
        assert row["move_result"]["result"]["result"] == "SUCCESS"
        assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
    assert await flow.rows() == retained
    assert len(flow.peer.requests) == len(flow.peer.commands) == 2
    for raw in flow.peer.requests:
        envelope = json.loads(raw)
        data = envelope["data"]
        assert data["barcode"] == flow.event()["data"]["barcode"]
        assert set(data) == {"workline_code", "barcode", "reel_diameter", "reel_thickness"}
        assert type(envelope["timestamp"]) is int
    for row in retained:
        assert row["timestamp"] == flow.event()["timestamp"]
    for raw in flow.peer.commands:
        command = json.loads(raw)
        assert command["task_type"] == "MOVE_FORWARD"
        assert command["timeout"] == 30000
        assert command["params"] == {"barcode": flow.event()["data"]["barcode"]}


async def test_concrete_worker_assembles_formal_outbox_without_sender(coarse_forward, monkeypatch):
    import asyncio

    from app.core import get_settings
    from app.reliable.sender import Sender
    from app.tasks import coarse_sorting_tasks as tasks

    flow = coarse_forward
    settings = get_settings().model_copy(
        update={
            "DATABASE_URL": flow.factory.kw["bind"].url.render_as_string(hide_password=False),
            "RELIABLE_TARGETS": {
                "targets": {
                    "coarse-wms": {
                        "base_url": flow.outbox.targets["coarse-wms"].base_url,
                        "protocol": "wms-decisions",
                    }
                }
            },
        }
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("business worker must not construct another Sender")

    monkeypatch.setattr(Sender, "__init__", forbidden)
    assert (await flow.post(flow.event())).status_code == 200
    assert await tasks.advance_pending(flow.factory) == 1
    assert (await flow.rows())[0]["validation_message_id"] is not None
    assert await flow.dispatch() == 1
    assert await asyncio.to_thread(tasks.advance_coarse_sorting.run) == 1
    row = (await flow.rows())[0]
    assert row["command_code"] is not None
    assert await flow.dispatch() == 1
    assert (await flow.result(row)).status_code == 200
    assert await asyncio.to_thread(tasks.advance_coarse_sorting.run) == 1
    assert (await flow.rows())[0]["wait_reason"] == "AWAITING_ALLOCATION_STAGE"
    assert await tasks.advance_pending(flow.factory) == 0


@pytest.mark.parametrize(
    "decision,code,status,expected",
    [
        ({"result": "NG", "reason_code": "BARCODE_INVALID"}, "DECIDED", 200, "AWAITING_NG_STAGE"),
        ({"result": "OK"}, "DECIDED", 200, "VALIDATION_RECONCILIATION_REQUIRED"),
        ({"result": "OK", "binding_id": ""}, "DECIDED", 200, "VALIDATION_RECONCILIATION_REQUIRED"),
        ({"result": "UNKNOWN"}, "DECIDED", 200, "VALIDATION_RECONCILIATION_REQUIRED"),
        (
            {"result": "OK", "binding_id": "B", "reason_code": "NG"},
            "DECIDED",
            200,
            "VALIDATION_RECONCILIATION_REQUIRED",
        ),
        ({"reason_code": "INVALID_DATA"}, "REJECTED", 422, "VALIDATION_RECONCILIATION_REQUIRED"),
        ({}, "PREPARE_ACCEPTED", 202, "VALIDATION_RECONCILIATION_REQUIRED"),
        ({}, "UNKNOWN", 200, "VALIDATION_RECONCILIATION_REQUIRED"),
    ],
)
async def test_only_valid_original_wms_ok_creates_command(
    coarse_forward, decision, code, status, expected
):
    flow = coarse_forward
    flow.peer.decision, flow.peer.decision_code, flow.peer.decision_status = decision, code, status
    assert (await flow.post(flow.event())).status_code == 200
    row = (await flow.rows())[0]
    await flow.service.advance(row["id"])
    original = (await flow.rows())[0]
    await flow.dispatch()
    assert await flow.service.advance(row["id"]) == expected
    after = (await flow.rows())[0]
    assert after["command_code"] is None
    assert after["pending"] is True
    assert after["validation_operation_id"] == original["validation_operation_id"]
    assert after["validation_message_id"] == original["validation_message_id"]
    if expected == "AWAITING_NG_STAGE":
        assert after["validation_response"]["response_data"] == decision
    flow.rebuild()
    assert await flow.service.advance(row["id"]) == expected
    assert (await flow.rows())[0]["command_code"] is None
    assert flow.peer.commands == []


@pytest.mark.parametrize(
    "changes",
    [
        {
            "result": "FAILED",
            "data": {},
            "error_detail": {"code": "BLOCKED", "msg": "mock failure"},
        },
        {"data": {}},
        {"data": {"actual_qty": 1}},
        {"data": {"location": "STATION_PIPELINE1_OUTPUT1"}},
    ],
)
async def test_applied_incomplete_or_failed_result_preserves_evidence_without_completion(
    coarse_forward, changes
):
    flow = coarse_forward
    row = await flow.prepare_command()
    await flow.dispatch()
    assert (await flow.result(row, **changes)).status_code == 200
    assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    saved = (await flow.rows())[0]
    assert saved["move_result"]["result"]["data"] == changes["data"]
    assert saved["command_code"] == row["command_code"]
    flow.rebuild()
    assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    assert await flow.dispatch() == 0
    assert len(flow.peer.commands) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"data": {"actual_qty": True, "location": "STATION_PIPELINE1_OUTPUT1"}},
        {"data": {"actual_qty": 2, "location": "STATION_PIPELINE1_OUTPUT1"}},
        {"data": {"actual_qty": 1, "location": "OTHER"}},
        {"device_code": "WRONG_DEVICE"},
        {"command_code": "MISSING_COMMAND"},
    ],
)
async def test_invalid_or_wrong_identity_result_cannot_complete_original_scan(
    coarse_forward, changes
):
    flow = coarse_forward
    row = await flow.prepare_command()
    await flow.dispatch()
    response = await flow.result(row, **changes)
    assert response.status_code in {400, 404, 409}, response.text
    assert await flow.service.advance(row["id"]) != "AWAITING_ALLOCATION_STAGE"
    assert (await flow.rows())[0]["move_result"] is None
    assert (await flow.rows())[0]["command_code"] == row["command_code"]
    assert await flow.dispatch() == 0


async def test_result_before_ack_and_receipt_cleanup_keep_original_move(coarse_forward):
    import asyncio

    from sqlalchemy import delete

    from app.models import CallbackReceipt

    flow = coarse_forward
    row = await flow.prepare_command()
    flow.peer.command = "hold_ack"
    sending = asyncio.create_task(flow.dispatch())
    try:
        await asyncio.wait_for(flow.peer.started.wait(), 5)
        assert (await flow.result(row)).status_code == 200
        assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
    finally:
        flow.peer.release.set()
        await sending
    retained = (await flow.rows())[0]
    async with flow.factory.begin() as session:
        await session.execute(delete(CallbackReceipt))
    flow.rebuild()
    assert (await flow.post(flow.event())).status_code == 200
    assert (await flow.result(row)).status_code == 200
    assert await flow.service.advance(row["id"]) == "AWAITING_ALLOCATION_STAGE"
    assert (await flow.rows())[0] == retained
    assert await flow.dispatch() == 0
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


async def test_unknown_ecs_delivery_keeps_original_command(coarse_forward):
    flow = coarse_forward
    row = await flow.prepare_command()
    flow.peer.command = "invalid"
    await flow.dispatch()
    assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    flow.rebuild()
    assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    assert (await flow.rows())[0]["command_code"] == row["command_code"]
    assert await flow.dispatch() == 0


@pytest.mark.parametrize("stage", ["validation", "command"])
async def test_missing_original_outcome_never_registers_replacement(coarse_forward, stage):
    from sqlalchemy import delete

    from app.models import EcsCommand, ReliableMessage

    flow = coarse_forward
    if stage == "command":
        row = await flow.prepare_command()
        async with flow.factory.begin() as session:
            await session.execute(
                delete(EcsCommand).where(EcsCommand.command_code == row["command_code"])
            )
    else:
        assert (await flow.post(flow.event())).status_code == 200
        await flow.service.advance((await flow.rows())[0]["id"])
        row = (await flow.rows())[0]
        async with flow.factory.begin() as session:
            await session.execute(
                delete(ReliableMessage).where(ReliableMessage.id == row["validation_message_id"])
            )
    before = await flow.outbound_counts()
    for _ in range(2):
        assert "RECONCILIATION" in await flow.service.advance(row["id"])
    after = (await flow.rows())[0]
    assert after["validation_operation_id"] == row["validation_operation_id"]
    assert after["command_code"] == row["command_code"]
    assert await flow.outbound_counts() == before


@pytest.mark.parametrize(
    "change", ["line_version", "input_version", "conveyor_version", "conveyor_rebind", "business"]
)
async def test_configuration_changes_stop_original_scan_on_original_identity(
    coarse_forward, change
):
    from sqlalchemy import select

    from app.models import Device, WorkLine

    flow = coarse_forward
    assert (await flow.post(flow.event())).status_code == 200
    await flow.service.advance((await flow.rows())[0]["id"])
    row = (await flow.rows())[0]
    await flow.dispatch()
    async with flow.factory.begin() as session:
        line = await session.get(WorkLine, row["work_line_id"])
        if change == "line_version":
            line.version += 1
        elif change == "business":
            line.business_code = None
        elif change == "conveyor_rebind":
            line.device_bindings = {**line.device_bindings, "CONVEYOR": flow.conveyor_codes[1]}
        else:
            code = row["device_code"] if change == "input_version" else row["conveyor_device_code"]
            device = await session.scalar(select(Device).where(Device.device_code == code))
            device.version += 1
    assert await flow.service.advance(row["id"]) in {
        "ORIGINAL_BINDING_CHANGED",
        "WORKLINE_BUSINESS_MISMATCH",
    }
    after = (await flow.rows())[0]
    assert after["command_code"] is None
    assert after["validation_operation_id"] == row["validation_operation_id"]
    assert after["conveyor_device_code"] == row["conveyor_device_code"]


async def test_pending_pages_skip_blocked_earlier_work_and_continue_after_error(
    coarse_forward, monkeypatch, caplog
):
    from app.businesses.coarse_sorting.service import CoarseSortingService

    flow = coarse_forward
    for timestamp in range(10):
        assert (await flow.post(flow.event(timestamp=timestamp))).status_code == 200
    ids = [row["id"] for row in await flow.rows()]
    original = CoarseSortingService.advance

    async def failure(service, scan_id):
        if scan_id == ids[0]:
            raise RuntimeError("private payload must not be logged")
        return await original(service, scan_id)

    monkeypatch.setattr(CoarseSortingService, "advance", failure)
    assert await flow.service.scan_pending(limit=3) == 10
    rows = await flow.rows()
    assert rows[0]["validation_message_id"] is None
    assert all(row["validation_message_id"] for row in rows[1:])
    assert ids[0] in caplog.text
    assert "private payload must not be logged" not in caplog.text
    assert await flow.service.scan_pending(limit=3) == 10
    assert await flow.rows() == rows


@pytest.mark.parametrize("phase", ["validation", "command", "result"])
async def test_storage_failure_rolls_back_consumption_and_next_outbox_together(
    coarse_forward, phase
):
    from sqlalchemy import event

    flow = coarse_forward
    if phase == "result":
        row = await flow.prepare_command()
        await flow.dispatch()
        assert (await flow.result(row)).status_code == 200
    else:
        assert (await flow.post(flow.event())).status_code == 200
        row = (await flow.rows())[0]
        if phase == "command":
            await flow.service.advance(row["id"])
            await flow.dispatch()
    before = (await flow.rows())[0]
    counts = await flow.outbound_counts()
    engine = flow.factory.kw["bind"].sync_engine

    def fail(connection, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and getattr(compiled.statement, "table", None) is flow.table:
            if compiled.statement.is_update:
                raise RuntimeError("injected write failure")

    event.listen(engine, "after_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected write failure"):
            await flow.service.advance(row["id"])
    finally:
        event.remove(engine, "after_cursor_execute", fail)
    assert (await flow.rows())[0] == before
    assert await flow.outbound_counts() == counts
    expected = {
        "validation": "AWAITING_VALIDATION_RESULT",
        "command": "AWAITING_MOVE_RESULT",
        "result": "AWAITING_ALLOCATION_STAGE",
    }[phase]
    assert await flow.service.advance(row["id"]) == expected


@pytest.mark.parametrize("exhaust", [False, True])
async def test_real_serialization_conflicts_retry_fresh_ordered_rr_transactions(
    coarse_forward, monkeypatch, exhaust
):
    from sqlalchemy import event, update
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    flow = coarse_forward
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL serialization and row-lock contract")
    assert (await flow.post(flow.event())).status_code == 200
    row = (await flow.rows())[0]
    sessions, statements = [], []
    execute = AsyncSession.execute

    async def collide(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if (
            getattr(statement, "is_select", False)
            and getattr(statement, "_for_update_arg", None) is None
            and flow.table in statement.get_final_froms()
            and (await session.connection())
            .sync_connection.get_execution_options()
            .get("isolation_level")
            == "REPEATABLE READ"
        ):
            sessions.append(session)
            if len(sessions) == 1 or exhaust:
                async with flow.factory.begin() as writer:
                    await writer.execute(
                        update(flow.table)
                        .where(flow.table.c.id == row["id"])
                        .values(wait_reason=f"EXTERNAL_WRITE_{len(sessions)}")
                    )
        return result

    def observe(connection, cursor, statement, parameters, context, executemany):
        if connection.get_execution_options().get("isolation_level") == "REPEATABLE READ":
            statements.append((connection, statement))

    monkeypatch.setattr(AsyncSession, "execute", collide)
    engine = flow.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        if exhaust:
            with pytest.raises(DBAPIError) as caught:
                await flow.service.advance(row["id"])
            assert caught.value.orig.sqlstate == "40001"
        else:
            assert await flow.service.advance(row["id"]) == "AWAITING_VALIDATION_RESULT"
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(sessions) == (3 if exhaust else 2)
    assert len({id(session) for session in sessions}) == len(sessions)
    assert all(not session.in_transaction() for session in sessions)
    for connection in {c for c, sql in statements}:
        sql = [s for c, s in statements if c is connection]
        assert flow.table.name in sql[0]
        locked = [s for s in sql if "FOR UPDATE" in s]
        assert "work_lines" in locked[0]
        assert "devices" in locked[1] and "ORDER BY" in locked[1]
        assert flow.table.name in locked[2]
    saved = (await flow.rows())[0]
    assert saved["pending"] is True
    assert bool(saved["validation_message_id"]) is not exhaust


async def test_concurrent_wakes_share_one_validation_and_command(
    coarse_forward, monkeypatch, wait_for_lock
):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    flow = coarse_forward
    if flow.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL locking contract")
    assert (await flow.post(flow.event())).status_code == 200
    row = (await flow.rows())[0]
    entered, release = asyncio.Event(), asyncio.Event()
    execute = AsyncSession.execute

    async def hold(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        if (
            getattr(statement, "_for_update_arg", None) is not None
            and flow.table in statement.get_final_froms()
            and not entered.is_set()
        ):
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", hold)
    first = asyncio.create_task(flow.service.advance(row["id"]))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(flow.service.advance(row["id"]))
        await wait_for_lock(second)
        assert not second.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(first, *([second] if second else []))
    assert outcomes == ["AWAITING_VALIDATION_RESULT"] * 2
    assert await flow.dispatch() == 1
    outcomes = await asyncio.gather(*(flow.service.advance(row["id"]) for _ in range(3)))
    assert outcomes == ["AWAITING_MOVE_RESULT"] * 3
    assert await flow.dispatch() == 1
    assert len(flow.peer.requests) == len(flow.peer.commands) == 1


async def test_result_before_dispatch_is_not_physical_completion(coarse_forward):
    flow = coarse_forward
    row = await flow.prepare_command()
    assert (await flow.result(row)).status_code == 409
    assert await flow.service.advance(row["id"]) == "MOVE_RECONCILIATION_REQUIRED"
    assert (await flow.rows())[0]["move_result"] is None
    assert await flow.dispatch() == 0


async def test_missing_target_configuration_rolls_back_and_reports_scan_identity(
    coarse_forward, caplog
):
    flow = coarse_forward
    flow.outbox.targets.pop("coarse-wms")
    assert (await flow.post(flow.event())).status_code == 200
    original = (await flow.rows())[0]
    assert await flow.service.scan_pending() == 1
    assert (await flow.rows())[0] == original
    assert original["id"] in caplog.text
    assert "Coarse sorting advance failed" in caplog.text
    assert flow.peer.requests == flow.peer.commands == []


@pytest.mark.parametrize(
    "status,uncertain,expected",
    [
        ("PENDING", False, "AWAITING_VALIDATION_RESULT"),
        ("SENDING", False, "AWAITING_VALIDATION_RESULT"),
        ("HELD", True, "VALIDATION_RECONCILIATION_REQUIRED"),
        ("EXPIRED", False, "VALIDATION_RECONCILIATION_REQUIRED"),
        ("DONE", False, "VALIDATION_RECONCILIATION_REQUIRED"),
    ],
)
async def test_validation_delivery_without_a_decision_retains_original_request(
    coarse_forward, status, uncertain, expected
):
    from sqlalchemy import update

    from app.models import ReliableMessage

    flow = coarse_forward
    assert (await flow.post(flow.event())).status_code == 200
    await flow.service.advance((await flow.rows())[0]["id"])
    original = (await flow.rows())[0]
    delivery = {"status": status, "delivery_uncertain": uncertain}
    if status == "HELD":
        delivery.update(next_attempt_at=None, pause_reason="DELIVERY_UNKNOWN")
    async with flow.factory.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == original["validation_message_id"])
            .values(**delivery)
        )
    counts = await flow.outbound_counts()
    for _ in range(2):
        flow.rebuild()
        assert await flow.service.advance(original["id"]) == expected
        saved = (await flow.rows())[0]
        assert saved["validation_operation_id"] == original["validation_operation_id"]
        assert saved["validation_message_id"] == original["validation_message_id"]
        assert saved["command_code"] is None
        assert saved["pending"] is True
    assert await flow.outbound_counts() == counts
    assert flow.peer.requests == flow.peer.commands == []
