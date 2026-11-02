"""Original owner notifications follow durable application, never current task lookup."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import business_wakeups
from app.core.identifiers import uuid7
from app.http_client import DeliveryState, HttpResult
from app.integrations.wms_transport import Rack, RackPosition, TransportCaller
from app.models import ReliableMessage
from app.reliable.store import AttemptOutcome
from app.reliable.types import FailReason, MessageStatus, Verdict, VerdictKind


@pytest.fixture
async def notices(formal_manual, monkeypatch):
    flow = formal_manual
    monkeypatch.setattr(business_wakeups, "enqueue_business", flow.notices.append)
    return flow


async def rack_transport(flow, *, owner=True):
    from app.integrations.wms_transport import TransportService

    transport = flow.host.capabilities.transport
    if not owner:
        transport = TransportService(transport.outbox)
    async with flow.factory.begin() as session:
        return await transport.move_rack(
            session,
            client_request_id=str(uuid7()),
            caller=TransportCaller(str(flow.lines[0])),
            rack_id=flow.prefix + "R",
            source=Rack(flow.prefix + "R"),
            target=RackPosition(flow.positions[0]["FIVE_RACK"]),
        )


@pytest.mark.parametrize("http,code", [(202, "RECEIVED"), (200, "DUPLICATE")])
async def test_transport_acceptance_wakes_original_manual_owner(notices, http, code):
    flow = notices
    flow.transport_http, flow.transport_code = http, code
    handle = await rack_transport(flow)
    claims = await flow.store.claim(100, flow.sender.targets)
    claim = next(c for c in claims if c.message.business_key == handle.transport_task_id)
    raw = json.dumps(
        dict(
            operation_id=claim.message.operation_id,
            code=code,
            timestamp=1,
            data={"transport_task_id": handle.transport_task_id},
        )
    ).encode()
    outcome = AttemptOutcome(
        MessageStatus.DONE,
        result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, http, (), raw),
        verdict=Verdict(VerdictKind.DELIVERED, response=raw),
    )
    assert not await flow.store.finish(replace(claim, lease_id="stale"), outcome)
    assert flow.notices == []
    assert await flow.store.finish(claim, outcome)
    assert flow.notices == ["smt_manual"]
    assert not await flow.store.finish(claim, outcome)
    assert flow.notices == ["smt_manual"]
    async with flow.factory() as session:
        saved = await session.get(ReliableMessage, claim.message_id)
        assert saved.status == "DONE" and saved.response_raw == raw


async def test_transport_acceptance_waits_for_outer_commit_and_rollback(notices):
    from app.transactions import after_commit

    flow = notices
    handle = await rack_transport(flow)
    claim = next(
        c
        for c in await flow.store.claim(100, flow.sender.targets)
        if c.message.business_key == handle.transport_task_id
    )
    for rollback in (True, False):
        async with flow.factory() as session:
            await business_wakeups.transport_submit_delivered(session, claim.message)
            nested = await session.begin_nested()
            after_commit(session, lambda: None)
            await nested.commit()
            assert flow.notices == []
            await session.rollback() if rollback else await session.commit()
    assert flow.notices == ["smt_manual"]


@pytest.mark.parametrize("case", ["UNKNOWN", "FAILED", "NO_OWNER"])
async def test_transport_unknown_and_missing_owner_do_not_notify(notices, case):
    flow = notices
    handle = await rack_transport(flow, owner=case != "NO_OWNER")
    claim = next(
        c
        for c in await flow.store.claim(100, flow.sender.targets)
        if c.message.business_key == handle.transport_task_id
    )
    raw = json.dumps(
        dict(
            operation_id=claim.message.operation_id,
            code="RECEIVED",
            timestamp=1,
            data={"transport_task_id": handle.transport_task_id},
        )
    ).encode()
    if case == "UNKNOWN":
        outcome = AttemptOutcome(
            MessageStatus.PENDING,
            result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), b"{}"),
            verdict=Verdict(VerdictKind.UNKNOWN),
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
        )
    elif case == "FAILED":
        outcome = AttemptOutcome(
            MessageStatus.FAILED,
            fail_reason=FailReason.REJECTED,
            result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 422, (), b"{}"),
            verdict=Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED),
        )
    else:
        outcome = AttemptOutcome(
            MessageStatus.DONE,
            result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw),
            verdict=Verdict(VerdictKind.DELIVERED, response=raw),
        )
    assert await flow.store.finish(claim, outcome)
    assert flow.notices == []


async def test_wms_ecs_and_transport_result_wake_original_manual_owner(notices):
    flow = notices
    task = flow.prefix + "-task"
    assert (await flow.issue(task)).status_code == 202
    await flow.host.advance("TASK", task)
    flow.notices.clear()
    await flow.dispatch()
    assert flow.notices == ["smt_manual"]
    await flow.host.advance("TASK", task)
    assert (await flow.scan("SCAN3", "A000000001-B")).status_code == 200
    scan = (await flow.rows("scan_arrival", roller=True))[0]
    await flow.dispatch()
    assert (
        await flow.callback("sorting.task.cancel@v1", dict(task_id=task, cancel_scope="TASK"))
    ).status_code == 202
    successor = task + "-new"
    assert (await flow.issue(successor)).status_code == 202
    await flow.host.advance("TASK", successor)
    flow.notices.clear()
    assert (await flow.result(scan["command_code"], rollback=True)).status_code == 200
    assert flow.notices == []
    assert (await flow.result(scan["command_code"])).status_code == 200
    assert flow.notices == ["smt_manual"]
    assert (await flow.result(scan["command_code"])).status_code == 200
    assert flow.notices == ["smt_manual"]
    handle = await rack_transport(flow)
    data = dict(
        transport_task_id=handle.transport_task_id,
        kind="RACK_MOVE",
        rack_id=flow.prefix + "R",
        outcome_revision=1,
        status="SUCCEEDED",
        final_position={"kind": "RACK_POSITION", "location_code": flow.positions[0]["FIVE_RACK"]},
    )
    flow.notices.clear()
    assert (
        await flow.callback("transport.task.resulted@v1", data, rollback=True)
    ).status_code == 202
    assert flow.notices == []
    assert (await flow.callback("transport.task.resulted@v1", data)).status_code == 202
    assert flow.notices == ["smt_manual"]
    assert (await flow.callback("transport.task.resulted@v1", data)).status_code in {200, 202}
    assert flow.notices == ["smt_manual"]


async def test_ecs_unapplied_result_does_not_wake_manual_owner(notices):
    flow = notices
    await flow.scan("SCAN3", "A000000001-B")
    scan = (await flow.rows("scan_arrival", roller=True))[0]
    flow.notices.clear()
    assert (await flow.result(scan["command_code"])).status_code == 409
    assert flow.notices == []


async def test_ctu03_acceptance_releases_cycle_without_final_outcome(notices):
    from app.integrations.wms_transport import TransportCaller
    from tests.helpers import manual_departure_obligation

    flow = notices
    cycle, action = await manual_departure_obligation(flow)
    await flow.dispatch()
    assert flow.notices == ["smt_manual"]
    assert await flow.host.advance("RACK_CYCLE", cycle["id"]) == "RELEASED"
    async with flow.factory() as session:
        original = await flow.host.capabilities.transport.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=TransportCaller(str(flow.lines[0])),
        )
        assert original.outcome is None and original.outcome_revision is None
    assert (await flow.rows("rack_action", roller=True))[0]["status"] == "ACCEPTED"
    assert (await flow.rows("rack_cycle", roller=True))[0]["release_basis"][
        "outcome_revision"
    ] is None
