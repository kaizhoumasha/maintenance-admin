import pytest


async def test_successor_requires_original_completed_authority(
    manual_bins, manual_completion_tools
):
    flow = manual_bins
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "UNKNOWN"
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    async with flow.factory() as session:
        successor = await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        assert successor.state == "NONE"
        assert successor.evidence["task_id"] == "T1"
        assert successor.evidence["confirmation_operation_id"]
    assert (await flow.issue("T2")).status_code == 202
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "CHECKING"


async def test_original_applied_task_cancel_seals_successor(manual_flow):
    flow = manual_flow
    await flow.issue()
    await flow.prepare()
    raw = flow.message("cancel", dict(task_id="T1", cancel_scope="TASK"))
    assert (await flow.deliver(raw)).status_code == 202
    async with flow.factory() as session:
        context = await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        assert context.state == "NONE"
        assert context.evidence["cancel_operation_id"]
    await flow.issue("T2")
    async with flow.factory() as session:
        assert (
            await flow.ports.read_successor(session, work_line_id=flow.lines[0])
        ).state == "CHECKING"


async def test_wait_does_not_start_new_admission_when_disabled(manual_host):
    from sqlalchemy import update

    from app.models import WorkLine

    flow = manual_host
    await flow.ready()
    await flow.scan()
    arrival = (await flow.rows("manual_bin"))[0]["arrival_id"]
    await flow.decision(dict(result="WAIT", retry_after_ms=1))
    assert await flow.host.advance("MANUAL_BIN", arrival) == "WAIT"
    original = (await flow.rows("manual_bin"))[0]["admission_operation_id"]
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
        await session.execute(
            flow.tables["request"]
            .update()
            .where(flow.tables["request"].c.kind == "BIN_ADMISSION")
            .values(due_at_ms=0)
        )
    assert await flow.host.advance("MANUAL_BIN", arrival) == "WAIT"
    assert (await flow.rows("manual_bin"))[0]["admission_operation_id"] == original


async def test_unassigned_scan4_reaches_drain_through_original_scan_pending(
    manual_host, manual_completion_tools
):
    import importlib

    from app.businesses import catalog
    from app.integrations.wms_request import operation

    flow = manual_host
    wire = importlib.import_module(
        "app.businesses._components.bin_roller.request.workline_return_buffer_drain_rack_decide_v1"
    )
    if wire.OPERATION not in operation.operations():
        operation.request_operation(wire.OPERATION, operation.Endpoint.DECISIONS)(wire.DrainRequest)
    catalog.validate_business_contracts()
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.host.advance("TASK", "T1")
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.host.advance("TASK", "T1") == "COMPLETED"
    assert (await flow.scan("A000000001-B", point="SCAN3")).status_code == 200
    assert (await flow.scan("A000000001-B", point="SCAN4", timestamp=20)).status_code == 200
    scan = next(
        row for row in await flow.rows("scan_arrival", component=True) if row["point"] == "SCAN4"
    )
    await flow.result(scan["command_code"], point="SCAN4")
    assert not await flow.rows("drain", component=True)
    assert not await flow.rows("rack_cycle", component=True)
    assert await flow.host.scan_pending(limit=1) >= 1
    drains = await flow.rows("drain", component=True)
    assert len(drains) == 1
    import json

    from app.models import ReliableMessage

    async with flow.factory() as session:
        message = await session.get(ReliableMessage, drains[0]["message_id"])
        assert json.loads(message.request_raw)["data"] == dict(
            workline_code="SMT-M1-1", required_slot_count=1
        )
    original = drains[0]["operation_id"]
    await flow.host.scan_pending(limit=1)
    assert [d["operation_id"] for d in await flow.rows("drain", component=True)] == [original]


@pytest.mark.parametrize(
    "decision",
    [
        dict(result="COMPLETED"),
        dict(result="PLAN_REVISION_STALE", current_plan_revision=1),
        dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=1),
    ],
)
async def test_cancelled_task_stops_original_confirmation_consumption(
    manual_host, manual_completion_tools, decision
):
    flow = manual_host
    flow.host.first_plan_wait_ms = 1
    await flow.ready()
    flow.test_clock[0] = 1001
    assert await flow.host.advance("TASK", "T1") == "PREPARING"
    original = (await flow.rows("confirmation"))[0]
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    await flow.confirmation_response(decision)
    assert await flow.host.scan_pending(limit=1) == 0
    task = (await flow.rows("task"))[0]
    assert task["status"] == "CANCELLED"
    current = (await flow.rows("confirmation"))[0]
    assert current["operation_id"] == original["operation_id"]
    assert current["decision_code"] is None and current["state"] == "REQUESTED"
    assert await flow.host.scan_pending(limit=1) == 0
    assert len(await flow.confirmations()) == 1


async def test_task_cancel_stops_wait_without_reparenting_on_switch(manual_host):
    from sqlalchemy import update

    from app.models import WorkLine

    flow = manual_host
    await flow.ready()
    await flow.scan()
    arrival = (await flow.rows("manual_bin"))[0]
    await flow.decision(dict(result="WAIT", retry_after_ms=1))
    await flow.host.advance("MANUAL_BIN", arrival["arrival_id"])
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    await flow.issue("T2")
    await flow.prepare()
    await flow.accept_prepare("T2")
    async with flow.factory.begin() as session:
        await session.execute(
            flow.tables["request"]
            .update()
            .where(flow.tables["request"].c.kind == "BIN_ADMISSION")
            .values(due_at_ms=0)
        )
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "CLOSED"
    current = (await flow.rows("manual_bin"))[0]
    assert (current["task_id"], current["bin_code"], current["scanned_at"]) == (
        "T1",
        arrival["bin_code"],
        arrival["scanned_at"],
    )
    assert len(await flow.rows("admission")) == 1
    async with flow.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == flow.lines[0]).values(is_enabled=False)
        )
    await flow.decision(dict(result="NO_WORK"))
    assert await flow.host.advance("MANUAL_BIN", arrival["arrival_id"]) == "CLOSED"
    assert (await flow.rows("manual_bin"))[0]["command_code"] is None


async def test_cancelled_task_keeps_original_late_prepare_result(manual_host):
    import json

    from sqlalchemy import update

    from app.models import ReliableMessage

    flow = manual_host
    await flow.issue()
    assert await flow.host.advance("TASK", "T1") == "PREPARING"
    original = (await flow.rows("task"))[0]
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    assert await flow.host.scan_pending() == 0
    async with flow.factory.begin() as session:
        await session.execute(
            update(ReliableMessage)
            .where(ReliableMessage.id == original["prepare_message_id"])
            .values(
                status="ABANDONED",
                last_http_status=202,
                last_verdict="DELIVERED",
                response_raw=json.dumps(
                    dict(
                        operation_id=original["prepare_operation_id"],
                        code="PREPARE_ACCEPTED",
                        timestamp=110,
                        data={},
                    )
                ).encode(),
            )
        )
    assert await flow.host.scan_pending() == 0
    current = (await flow.rows("task"))[0]
    assert current["status"] == "CANCELLED" and not current["prepare_accepted"]
    assert current["prepare_operation_id"] == original["prepare_operation_id"]
    assert await flow.host.scan_pending() == 0


async def test_conflicted_completed_response_does_not_seal_successor(manual_completion_tools):
    from sqlalchemy import update

    flow = manual_completion_tools
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    async with flow.factory.begin() as session:
        await session.execute(update(flow.tables["task"]).values(status="COMPLETED"))
    await flow.confirmation_response({"result": "COMPLETED"})
    await flow.confirm()
    request = (await flow.rows("confirmation"))[0]
    assert request["state"] == "REQUESTED" and request["decision_code"] is None
    async with flow.factory() as session:
        successor = await flow.ports.read_successor(session, work_line_id=flow.lines[0])
    assert successor.state == "UNKNOWN"
