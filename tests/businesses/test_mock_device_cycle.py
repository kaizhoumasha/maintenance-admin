"""Device-triggered cycles retain one execution across receivers and binding changes."""

import asyncio
import json
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.main import app
from app.models import CallbackReceipt, WorkLine
from tests.helpers import flow_request


@pytest.mark.parametrize("first_kind", ["ecs", "wms"])
async def test_cross_entry_replay_and_conflict_keep_one_original_cycle(mock_cycle, first_kind):
    event_path, event = flow_request(mock_cycle, kind="ecs")
    identity = event["data"]["execution_id"]
    wms_path, document = flow_request(mock_cycle, kind="wms", execution_id=identity)
    entries = {"ecs": (event_path, event, 200), "wms": (wms_path, document, 202)}
    second_kind = "wms" if first_kind == "ecs" else "ecs"
    service = mock_cycle.runtime.services["seam_a"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path, body, status = entries[first_kind]
        assert (await client.post(path, json=body)).status_code == status
        assert await service.advance(identity) == "decision"
        original = deepcopy((await mock_cycle.rows())[0])
        for kind in (first_kind, second_kind):
            path, body, _ = entries[kind]
            assert (await client.post(path, json=body)).status_code == 200
        event["timestamp"] = 2
        assert (await client.post(event_path, json=event)).status_code == 200
        for kind in ("ecs", "wms"):
            path, body, _ = entries[kind]
            changed = deepcopy(body)
            changed["data"]["token"] = "changed"
            assert (await client.post(path, json=changed)).status_code == (
                200 if kind == "ecs" else 409
            )
            changed["data"]["device_code"] = mock_cycle.codes[1]
            assert (await client.post(path, json=changed)).status_code == (
                200 if kind == "ecs" else 409
            )
        assert await mock_cycle.rows() == [original]
    async with mock_cycle.factory() as session:
        receipts = list(
            await session.scalars(
                select(CallbackReceipt).where(CallbackReceipt.id.in_(mock_cycle.receipt_ids))
            )
        )
        assert any(
            note["reason_code"] == "EXECUTION_CONFLICT"
            for receipt in receipts
            if receipt.operation == "ecs.event"
            for note in receipt.diagnostic or []
        )
    rebuilt = mock_cycle.rebuild().services["seam_a"]
    assert await rebuilt.pending_execution_ids() == (identity,)
    assert await rebuilt.advance(identity) == "waiting"
    assert await mock_cycle.dispatch() == 1
    assert await rebuilt.advance(identity) == "command"
    assert await mock_cycle.dispatch() == 1
    assert await rebuilt.advance(identity) == "waiting"
    assert (await mock_cycle.result(identity)).status_code == 200
    assert await rebuilt.advance(identity) == "report"
    assert await mock_cycle.dispatch() == 1
    assert await rebuilt.advance(identity) == "done"
    assert await rebuilt.advance(identity) == "done"
    assert await mock_cycle.dispatch() == 0
    assert [name for name, _ in mock_cycle.peer.received] == ["decisions", "command", "facts"]


async def test_result_before_ack_and_repeated_event_never_replace_the_command(mock_cycle):
    path, event = flow_request(mock_cycle, kind="ecs")
    identity = event["data"]["execution_id"]
    service = mock_cycle.runtime.services["seam_a"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=event)).status_code == 200
        assert await service.advance(identity) == "decision"
        await mock_cycle.dispatch()
        assert await service.advance(identity) == "command"
        original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["command"])
        mock_cycle.peer.command = "hold_ack"
        dispatch = asyncio.create_task(mock_cycle.dispatch())
        try:
            await asyncio.wait_for(mock_cycle.peer.command_started.wait(), timeout=5)
            assert await service.advance(identity) == "waiting"
            assert (await mock_cycle.result(identity)).status_code == 200
            assert (await mock_cycle.result(identity)).status_code == 200
            event["timestamp"] = 3
            assert (await client.post(path, json=event)).status_code == 200
            assert await service.advance(identity) == "report"
        finally:
            mock_cycle.peer.release_ack.set()
            await dispatch
    assert (await mock_cycle.rows())[0]["next_intent"]["command"] == original
    assert (await mock_cycle.result(identity)).status_code == 200
    await mock_cycle.dispatch()
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "done"
    assert [name for name, _ in mock_cycle.peer.received] == ["decisions", "command", "facts"]


@pytest.mark.parametrize("wrong", ["payload_device", "role_payload", "business"])
async def test_device_entry_rejects_cross_line_and_wrong_role_without_business_work(
    mock_cycle, wrong
):
    path, event = flow_request(mock_cycle, kind="ecs")
    if wrong == "payload_device":
        event["data"]["device_code"] = mock_cycle.codes[1]
    elif wrong == "role_payload":
        event["device_code"] = mock_cycle.codes[2]
    else:
        async with mock_cycle.factory.begin() as session:
            await session.execute(
                update(WorkLine)
                .where(WorkLine.id == mock_cycle.line_ids[0])
                .values(business_code="seam_b")
            )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.post(path, json=event)
    assert reply.status_code == (400 if wrong == "role_payload" else 200)
    assert await mock_cycle.rows() == []
    assert await mock_cycle.runtime.services["seam_a"].pending_execution_ids() == ()
    assert await mock_cycle.dispatch() == 0
    assert mock_cycle.peer.received == []
    if wrong == "payload_device":
        async with mock_cycle.factory() as session:
            receipt = await session.scalar(
                select(CallbackReceipt).where(CallbackReceipt.id.in_(mock_cycle.receipt_ids))
            )
            assert receipt.diagnostic == [{"reason_code": "DEVICE_MISMATCH"}]


async def test_frozen_target_survives_position_drift_and_binding_gate_retains_original_result(
    mock_cycle,
):
    path, event = flow_request(mock_cycle, kind="ecs")
    identity = event["data"]["execution_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=event)).status_code == 200
    async with mock_cycle.factory.begin() as session:
        await session.execute(
            update(WorkLine)
            .where(WorkLine.id == mock_cycle.line_ids[0])
            .values(position_bindings={"INLET": "CHANGED_POSITION"})
        )
    service = mock_cycle.rebuild().services["seam_a"]
    assert await service.advance(identity) == "decision"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "command"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"])
    await mock_cycle.dispatch()
    async with mock_cycle.factory.begin() as session:
        await session.execute(
            update(WorkLine).where(WorkLine.id == mock_cycle.line_ids[0]).values(device_bindings={})
        )
    assert (await mock_cycle.result(identity)).status_code == 200
    assert await service.advance(identity) == "ORIGINAL_BINDING_CHANGED"
    row = (await mock_cycle.rows())[0]
    assert row["next_intent"] == original and row["pending"] and not row["done"]
    assert await mock_cycle.dispatch() == 0
    async with mock_cycle.factory.begin() as session:
        await session.execute(
            update(WorkLine)
            .where(WorkLine.id == mock_cycle.line_ids[0])
            .values(device_bindings={"SEAM_A": mock_cycle.codes[0]})
        )
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "report"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "done"
    decision, command, report = [json.loads(raw) for _, raw in mock_cycle.peer.received]
    assert decision["data"]["position"] == report["data"]["position"] == "POSITION_0"
    assert command["params"] == {"position": "POSITION_0"}
    assert command["command_code"] == original["command"]["command_code"]


async def test_two_device_lines_apply_only_their_own_result_and_target(mock_cycle):
    identities = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for index in (0, 1):
            path, event = flow_request(mock_cycle, kind="ecs", index=index)
            assert (await client.post(path, json=event)).status_code == 200
            identities.append(event["data"]["execution_id"])
    service = mock_cycle.rebuild().services["seam_a"]
    for identity in identities:
        assert await service.advance(identity) == "decision"
    await mock_cycle.dispatch()
    for identity in identities:
        assert await service.advance(identity) == "command"
    await mock_cycle.dispatch()
    assert (
        await mock_cycle.result(identities[0], device_code=mock_cycle.codes[1])
    ).status_code == 404
    assert await service.advance(identities[0]) == "waiting"
    assert (await mock_cycle.result(identities[1])).status_code == 200
    assert await service.advance(identities[1]) == "report"
    await mock_cycle.dispatch()
    assert await service.advance(identities[1]) == "done"
    assert await service.advance(identities[0]) == "waiting"
    assert (await mock_cycle.result(identities[0])).status_code == 200
    assert await service.advance(identities[0]) == "report"
    await mock_cycle.dispatch()
    assert await service.advance(identities[0]) == "done"
    rows = {row["id"]: row for row in await mock_cycle.rows()}
    assert len({row["next_intent"]["command"]["command_code"] for row in rows.values()}) == 2
    for index, identity in enumerate(identities):
        row = rows[identity]
        expected = f"POSITION_{index}"
        assert row["next_intent"]["position"] == expected
        assert row["evidence"]["command"]["result"]["data"] == {"position": expected}
        assert row["evidence"]["command"]["result"]["device_code"] == mock_cycle.codes[index]
        for endpoint in ("decisions", "facts"):
            sent = [json.loads(raw) for name, raw in mock_cycle.peer.received if name == endpoint]
            assert {body["data"]["execution_id"]: body["data"]["position"] for body in sent} == {
                identities[0]: "POSITION_0",
                identities[1]: "POSITION_1",
            }


async def test_conflicting_result_retains_original_command_and_blocks_completion(mock_cycle):
    path, event = flow_request(mock_cycle, kind="ecs")
    identity = event["data"]["execution_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json=event)).status_code == 200
    service = mock_cycle.runtime.services["seam_a"]
    assert await service.advance(identity) == "decision"
    await mock_cycle.dispatch()
    assert await service.advance(identity) == "command"
    original = deepcopy((await mock_cycle.rows())[0]["next_intent"]["command"])
    await mock_cycle.dispatch()
    assert (await mock_cycle.result(identity)).status_code == 200
    assert (await mock_cycle.result(identity, position="CONFLICT")).status_code == 409
    assert await service.advance(identity) == "blocked"
    row = (await mock_cycle.rows())[0]
    assert row["pending"] and not row["done"] and "report" not in row["next_intent"]
    assert row["next_intent"]["command"] == original
    assert await mock_cycle.rebuild().services["seam_a"].advance(identity) == "blocked"
    assert await mock_cycle.dispatch() == 0
    assert [name for name, _ in mock_cycle.peer.received] == ["decisions", "command"]
