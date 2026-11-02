"""The manual host consumes the merged component round/FIFO rules unchanged."""

import pytest
from sqlalchemy import select

from app import business_wakeups
from app.models import ReliableMessage, TransportTask
from tests.helpers import manual_return_opportunity


@pytest.mark.parametrize("kind", ["departure", "drain"])
async def test_formal_rack_decisions_return_valid_wait(formal_manual, monkeypatch, kind):
    from app.businesses._components.bin_roller.request import (
        rack_departure_decide_v1 as departure,
    )
    from app.businesses._components.bin_roller.request import (
        workline_return_buffer_drain_rack_decide_v1 as drain,
    )
    from app.core.identifiers import uuid7

    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    expected = dict(result="WAIT", retry_after_ms=60000)
    if kind == "departure":
        contract = departure
        payload = departure.DepartureRequest(
            workline_code=flow.prefix + "-1",
            rack_id=flow.prefix + "-rack",
            current_location=dict(
                type="RACK_POSITION", location_code=flow.positions[0]["FIVE_RACK"]
            ),
            current_face=None,
        )
    else:
        contract = drain
        payload = drain.DrainRequest(workline_code=flow.prefix + "-1", required_slot_count=1)
        expected["reason_code"] = "NO_DRAIN_RACK_AVAILABLE"
    operation_id = str(uuid7())
    async with flow.factory.begin() as session:
        sent = await flow.host.capabilities.wms.send(session, payload, operation_id=operation_id)
    await flow.dispatch()
    async with flow.factory() as session:
        result = await flow.host.capabilities.wms.get_result(
            session,
            message_id=sent.message_id,
            operation=contract.OPERATION,
            operation_id=operation_id,
        )
    assert result.availability == "FOUND" and result.response_state == "VALID"
    assert result.http_status == 200 and result.response_code == "DECIDED"
    assert contract.DECISION.validate_python(result.response_data).model_dump() == expected


async def test_manual_host_preserves_new_scan3_round_and_old_obligations(
    formal_manual, monkeypatch
):
    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    await flow.dispatch()
    old3, old4 = await flow.rows("scan_arrival", roller=True)
    for row in (old3, old4):
        await flow.result(row["command_code"])
        await flow.host.advance("SCAN", row["id"])
    batch_id = await manual_return_opportunity(flow)
    flow.return_decision = dict(
        result="READY",
        moves=[
            dict(
                sequence_no=1,
                bin_code="A000000001",
                target=dict(
                    type="RACK_BIN_SLOT",
                    rack_id=flow.prefix + "-return",
                    rack_face="A",
                    slot_id="S1",
                ),
            )
        ],
    )
    await flow.dispatch()
    await flow.host.advance("RETURN_BATCH", batch_id)
    batch = (await flow.rows("return_batch", roller=True))[0]
    assert batch["transport_task_id"]
    async with flow.factory() as session:
        frozen_transport = await session.scalar(
            select(TransportTask.frozen).where(
                TransportTask.transport_task_id == batch["transport_task_id"]
            )
        )
        frozen_bytes = await session.scalar(
            select(ReliableMessage.request_raw).where(
                ReliableMessage.operation_id == batch["transport_operation_id"]
            )
        )
    await flow.scan("SCAN3", "A000000001-B", timestamp=20)
    arrivals = await flow.rows("scan_arrival", roller=True)
    new3 = arrivals[-1]
    assert new3["round_id"] != old3["round_id"]
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=flow.lines[0]) == ()
    await flow.scan("SCAN4", "A000000001-B", timestamp=21)
    new4 = (await flow.rows("scan_arrival", roller=True))[-1]
    assert new4["round_id"] == new3["round_id"]
    await flow.result(old4["command_code"])
    await flow.host.advance("SCAN", old4["id"])
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=flow.lines[0]) == ()
    await flow.dispatch()
    await flow.result(new4["command_code"])
    await flow.host.advance("SCAN", new4["id"])
    async with flow.factory() as session:
        assert [
            r["id"]
            for r in await flow.roller.get_return_candidates(session, work_line_id=flow.lines[0])
        ] == [new3["round_id"]]
        assert (
            await session.scalar(
                select(TransportTask.frozen).where(
                    TransportTask.transport_task_id == batch["transport_task_id"]
                )
            )
            == frozen_transport
        )
        assert (
            await session.scalar(
                select(ReliableMessage.request_raw).where(
                    ReliableMessage.operation_id == batch["transport_operation_id"]
                )
            )
            == frozen_bytes
        )
    current = (await flow.rows("return_batch", roller=True))[0]
    for key in (
        "members",
        "operation_id",
        "message_id",
        "client_request_id",
        "transport_task_id",
        "transport_operation_id",
    ):
        assert current[key] == batch[key]


async def test_scan4_retry_keeps_original_fifo_under_manual_host(formal_manual, monkeypatch):
    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    await flow.dispatch()
    first = (await flow.rows("return_round", roller=True))[0]
    old4 = (await flow.rows("scan_arrival", roller=True))[-1]
    await flow.result(old4["command_code"], status="FAILED")
    await flow.host.advance("SCAN", old4["id"])
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN4", "A000000002-B", timestamp=21)
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    await flow.dispatch()
    retry = [r for r in await flow.rows("scan_arrival", roller=True) if r["point"] == "SCAN4"][1:]
    for row in retry:
        await flow.result(row["command_code"])
        await flow.host.advance("SCAN", row["id"])
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=flow.lines[0])
        assert [r["bin_code"] for r in candidates] == ["A000000001", "A000000002"]
    current = next(
        r for r in await flow.rows("return_round", roller=True) if r["id"] == first["id"]
    )
    assert current["scan4_arrival_id"] == first["scan4_arrival_id"]
