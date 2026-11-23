import json
from dataclasses import replace
from time import time
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.businesses._components.bin_roller.ports import RollerWorkRef
from app.businesses.smt_auto.task_flow import supply_member_closed
from app.businesses.smt_manual.task_flow import SmtManualTaskFlow
from app.http_client import DeliveryState, HttpRequest, HttpResult
from app.integrations import history
from app.models import ReliableMessage
from app.reliable import Verdict, VerdictKind
from app.reliable.store import AttemptOutcome, MessageStore
from tests.helpers import set_message_bodies, update_message_bodies


async def setup_withdrawn(flow, *, acknowledge=True):
    await flow.ready()
    await flow.bin_plan()
    await flow.start_supply()
    (cycle,) = await flow.rows("rack_cycle", component=True)
    (action,) = await flow.rows("rack_action", component=True)
    if acknowledge:
        await flow.acknowledge(action)
    await flow.withdraw_supply()
    return cycle["id"], action


async def advance_cycle(flow, cycle):
    async with flow.factory.begin() as session:
        await flow.roller.advance(session, RollerWorkRef("RACK_CYCLE", cycle))


async def reply_departure(flow, departure, data):
    async with flow.factory.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == departure["message_id"]),
            status="DONE",
            last_http_status=200,
            last_verdict="DELIVERED",
            response_raw=json.dumps(
                dict(
                    operation_id=departure["operation_id"],
                    code="DECIDED",
                    timestamp=100,
                    data=data,
                )
            ).encode(),
        )


async def projected(flow):
    async with flow.factory() as session:
        (fact,) = await flow.roller.get_supply_progress(
            session, work_line_id=flow.lines[0], task_id="T1"
        )
    return fact


@pytest.mark.parametrize("timing", ["before_confirmation", "after_confirmation", "saved_conflict"])
async def test_late_arrival_handoff_allows_original_task_to_close(manual_production_supply, timing):
    flow = manual_production_supply
    cycle, action = await setup_withdrawn(flow)
    assert (
        await flow.rack_outcome(action, status="CANCELLED", location="STORE")
    ).status_code == 202
    await advance_cycle(flow, cycle)
    assert (await projected(flow)).fed_bin_count == 0
    issued_before_arrival = timing != "before_confirmation"
    original_id = None
    if issued_before_arrival:
        await flow.confirm()
        (original,) = await flow.confirmations()
        original_id = original.operation_id
    assert (await flow.rack_outcome(action, face="A", revision=2)).status_code == 202
    if issued_before_arrival:
        await flow.confirmation_response(dict(result="COMPLETED"))
        assert await flow.confirm() == "EXECUTING"
    if timing == "saved_conflict":
        table = flow.tables["request"]
        async with flow.factory.begin() as session:
            await session.execute(
                table.update()
                .where(table.c.operation_id == original_id)
                .values(
                    state="CONFLICT",
                    decision_code="COMPLETED",
                )
            )
    await flow.confirm()
    assert len(await flow.confirmations()) == int(issued_before_arrival)
    await advance_cycle(flow, cycle)
    (departure,) = await flow.rows("departure", component=True)
    await reply_departure(
        flow,
        departure,
        dict(
            result="READY",
            rack_destination=dict(type="ZONE", location_code="STORE"),
        ),
    )
    await advance_cycle(flow, cycle)
    actions = await flow.rows("rack_action", component=True)
    depart = next(a for a in actions if a["kind"] in {"DEPART", "CTU03"})
    await flow.acknowledge(depart)
    await advance_cycle(flow, cycle)
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert not job.pending
        assert any(a.kind == "DEPART" and a.phase == "ACCEPTED" for a in job.actions)
    fact = await projected(flow)
    assert not fact.pending and fact.fed_bin_count == 0
    if not issued_before_arrival:
        await flow.confirm()
        await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    (original,) = await flow.confirmations()
    if issued_before_arrival:
        assert original.operation_id == original_id
    request = next(r for r in await flow.rows("request") if r["message_id"] == original.id)
    assert request["state"] == "CONSUMED" and request["decision_code"] == "COMPLETED"


@pytest.mark.parametrize("state", ["bad_decision", "expired"])
async def test_terminal_unusable_confirmation_records_conflict_on_original(
    manual_bins, manual_completion_tools, state
):
    flow = manual_bins
    await flow.ready()
    await flow.bin_plan(cancel=True)
    await flow.confirm()
    (original,) = await flow.confirmations()
    if state == "bad_decision":
        await flow.confirmation_response(dict(result="BUSINESS_IN_PROGRESS", retry_after_ms=0))
    else:
        async with flow.factory.begin() as session:
            await session.execute(
                update(ReliableMessage)
                .where(ReliableMessage.id == original.id)
                .values(status="EXPIRED")
            )
    await flow.scan()
    await flow.confirm(now_ms=99999)
    (work,) = await flow.rows("manual_bin")
    await flow.advance("bin", work["arrival_id"])
    assert await flow.rows("admission") == []
    request = next(r for r in await flow.rows("request") if r["message_id"] == original.id)
    assert request["state"] == "CONFLICT"
    assert request["decision_code"] == (
        "INVALID_COMPLETION_DECISION" if state == "bad_decision" else "COMPLETION_DELIVERY_TERMINAL"
    )
    assert request["response"]["delivery_status"] == (
        "DONE" if state == "bad_decision" else "EXPIRED"
    )
    assert len(await flow.confirmations()) == 1

    async with flow.factory.begin() as session:
        assert not await flow.task.reconcile_completion(
            session,
            task_id="T1",
            operation_id="wrong",
            response_timestamp=200,
            decision_data={"result": "COMPLETED"},
            evidence_ref="wms-check-1",
        )
        assert await flow.task.reconcile_completion(
            session,
            task_id="T1",
            operation_id=original.operation_id,
            response_timestamp=200,
            decision_data={"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 100},
            evidence_ref="wms-check-1",
        )
    await flow.confirm(now_ms=100000)
    original_request = next(r for r in await flow.rows("request") if r["message_id"] == original.id)
    assert original_request["state"] == "WAITING"
    reconciliation = original_request["response"]["reconciliation"]
    assert reconciliation["evidence_ref"] == "wms-check-1"
    assert reconciliation["original_response"] == request["response"]
    assert len(await flow.confirmations()) == 1
    await flow.advance("bin", work["arrival_id"])
    assert len(await flow.rows("admission")) == 1


async def test_unrelated_direct_bin_does_not_count_for_missing_fed_bin(manual_production_supply):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan()
    demand = await flow.start_supply(feed=True)
    await flow.withdraw_supply()
    await flow.feed_success(demand)
    await flow.confirm()
    assert await flow.confirmations() == []
    assert (await flow.scan("A000000002-C", timestamp=10)).status_code == 200
    (box,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", box["arrival_id"])
    await flow.confirm()
    assert await flow.confirmations() == []
    assert (await flow.scan("A000000001-C", timestamp=20)).status_code == 200
    box = next(b for b in await flow.rows("manual_bin") if b["bin_code"] == "A000000001")
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", box["arrival_id"])
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


async def test_cancelled_supplied_bin_requires_its_own_downstream_bypass(manual_production_supply):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan()
    demand = await flow.start_supply(feed=True)
    await flow.feed_success(demand)
    assert (await flow.scan("A000000001-B", point="SCAN1", timestamp=90)).status_code == 200
    await flow.withdraw_supply()
    assert (await flow.scan("A000000002-B", point="SCAN3", timestamp=110)).status_code == 200
    await flow.confirm()
    assert await flow.confirmations() == []
    assert (await flow.scan("A000000001-B", point="SCAN3", timestamp=120)).status_code == 200
    await flow.confirm()
    (message,) = await flow.confirmations()
    request = next(r for r in await flow.rows("request") if r["message_id"] == message.id)
    ((_, bin_code, *evidence),) = request["basis"]["supply_bypasses"]
    assert bin_code == "A000000001" and all(evidence)
    assert await flow.rows("manual_bin") == []
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


@pytest.mark.parametrize("reply_after_completion", [False, True])
async def test_completed_task_still_retries_original_rack_departure_wait(
    manual_production_supply, monkeypatch, reply_after_completion
):
    flow = manual_production_supply
    await flow.ready()
    await flow.bin_plan()
    demand = await flow.start_supply(feed=True)
    (cycle_row,) = await flow.rows("rack_cycle", component=True)
    cycle = cycle_row["id"]
    assert (
        await flow.deliver(
            flow.message(
                "plan_delta",
                dict(
                    task_id="T1",
                    plan_revision=2,
                    added_bin_source_racks=[dict(rack_id="R", rack_face=["B"])],
                ),
            )
        )
    ).status_code == 202
    await flow.feed_success(demand)
    assert (await flow.scan("A000000001-C", timestamp=10)).status_code == 200
    (box,) = await flow.rows("manual_bin")
    await flow.decision(dict(result="NO_WORK"))
    await flow.advance("bin", box["arrival_id"])
    await advance_cycle(flow, cycle)
    action = next(
        a for a in await flow.rows("rack_action", component=True) if a["target_face"] == "B"
    )
    await flow.acknowledge(action)
    assert (
        await flow.deliver(
            flow.message(
                "cancel",
                dict(
                    task_id="T1",
                    cancel_scope="PLAN_MEMBERS",
                    bin_source_racks=[dict(plan_revision=2, rack_id="R", rack_face=["B"])],
                ),
            )
        )
    ).status_code == 202
    assert (await flow.rack_outcome(action, status="CANCELLED", location="FIVE")).status_code == 202
    await advance_cycle(flow, cycle)
    (departure,) = await flow.rows("departure", component=True)
    assert departure["operation_id"]
    wait = dict(result="WAIT", retry_after_ms=60000)
    if reply_after_completion:
        store, claim = await send_original(flow, departure["message_id"])
    else:
        await reply_departure(flow, departure, wait)
        await advance_cycle(flow, cycle)
    assert len(await flow.rows("departure", component=True)) == 1
    await flow.confirm()
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"
    if reply_after_completion:
        await record_late_response(store, claim, code="DECIDED", http=200, data=wait)
    await advance_cycle(flow, cycle)
    assert len(await flow.rows("departure", component=True)) == 1
    from app.businesses._components.bin_roller import rack_flow

    after_wait = time() + 61
    monkeypatch.setattr(rack_flow, "time", lambda: after_wait)
    await advance_cycle(flow, cycle)
    first, retry = await flow.rows("departure", component=True)
    assert first["operation_id"] == departure["operation_id"]
    assert retry["operation_id"] != first["operation_id"]
    await reply_departure(
        flow,
        retry,
        dict(
            result="READY",
            rack_destination=dict(type="ZONE", location_code="STORE"),
        ),
    )
    await advance_cycle(flow, cycle)
    (depart,) = [a for a in await flow.rows("rack_action", component=True) if a["kind"] == "CTU03"]
    await flow.acknowledge(depart)
    await advance_cycle(flow, cycle)
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert not job.pending and job.state == "CLOSED_BY_SCOPE"
        assert job.closure_basis["reason"] == "TASK_COMPLETED"
        assert job.closure_basis["responsibility_release"]["kind"] == "DEPARTURE_ACK"
    assert len(await flow.rows("departure", component=True)) == 2


async def test_withdrawn_known_failure_can_close_task(manual_production_supply):
    flow = manual_production_supply
    cycle, action = await setup_withdrawn(flow)
    assert (await flow.rack_outcome(action, status="FAILED", location="STORE")).status_code == 202
    await advance_cycle(flow, cycle)
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert job.state == "NOT_ARRIVED" and not job.pending
    (face,) = await flow.rows("face_work", component=True)
    assert face["close_reason"] == "WITHDRAWN" and face["feed_operation_id"] is None
    fact = await projected(flow)
    assert not fact.pending and fact.fed_bin_count == 0
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


async def test_withdrawn_explicitly_rejected_entry_can_close_task(manual_production_supply):
    flow = manual_production_supply
    cycle, action = await setup_withdrawn(flow, acknowledge=False)
    async with flow.factory.begin() as session:
        message = await session.get(ReliableMessage, action["submit_message_id"])
        message.status = "FAILED"
        message.last_http_status = 422
        message.last_verdict = "PEER_TERMINAL"
        message.fail_reason = "REJECTED"
        message.delivery_uncertain = False
        await set_message_bodies(
            session,
            message,
            response_raw=json.dumps(
                dict(
                    operation_id=message.operation_id,
                    code="REJECTED",
                    timestamp=2,
                    data=dict(
                        transport_task_id=action["transport_task_id"],
                        reason_code="INVALID_DATA",
                    ),
                )
            ).encode(),
        )
    (face,) = await flow.rows("face_work", component=True)
    async with flow.factory.begin() as session:
        await flow.roller.advance(session, RollerWorkRef("FACE", face["id"]))
    async with flow.factory() as session:
        job = await flow.roller.rack_dispatch.progress(session, job_id=cycle)
        assert job.state == "NOT_ARRIVED" and not job.pending
    fact = await projected(flow)
    assert not fact.pending and fact.fed_bin_count == 0
    await flow.confirm()
    assert len(await flow.confirmations()) == 1
    await flow.confirmation_response(dict(result="COMPLETED"))
    assert await flow.confirm() == "COMPLETED"


async def test_auto_can_consume_proven_zero_after_original_preparation_cancel(
    manual_production_supply,
):
    flow = manual_production_supply
    cycle, action = await setup_withdrawn(flow)
    assert (
        await flow.rack_outcome(action, status="CANCELLED", location="STORE")
    ).status_code == 202
    await advance_cycle(flow, cycle)
    fact = await projected(flow)
    assert fact.fed_bin_count == 0 and not fact.pending and fact.original_ids
    member = SimpleNamespace(
        task_id="T1",
        plan_revision=1,
        rack_id="R",
        rack_face="A",
        withdrawn_at_ms=100,
        withdrawn_by_input_id=1,
    )
    cancel = SimpleNamespace(
        id=1,
        task_id="T1",
        disposition="APPLIED",
        operation="sorting.task.cancel@v1",
        effective_request=dict(
            cancel_scope="PLAN_MEMBERS",
            bin_source_racks=[dict(plan_revision=1, rack_id="R", rack_face=["A"])],
        ),
    )
    assert not supply_member_closed(member, [fact], [])
    assert supply_member_closed(member, [fact], [cancel])
    await flow.confirm()
    assert len(await flow.confirmations()) == 1


@pytest.mark.parametrize(
    "decision,state",
    [
        ({"result": "COMPLETED"}, "CONFLICT"),
        ({"result": "PLAN_REVISION_STALE", "current_plan_revision": 1}, "CONSUMED"),
        ({"result": "BUSINESS_IN_PROGRESS", "retry_after_ms": 1}, "CONSUMED"),
    ],
)
async def test_cancelled_revision_zero_consumes_original_confirmation(
    manual_host, manual_completion_tools, decision, state
):

    flow = manual_completion_tools
    flow.task = SmtManualTaskFlow(
        flow.task.capabilities, flow.roller, repository=flow.task.repository, first_plan_wait_ms=100
    )
    await flow.ready()
    await flow.confirm(now_ms=1100)
    (original,) = await flow.confirmations()
    store, claim = await send_original(flow, original.id)
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    await manual_host.host.scan_pending(limit=1)
    request = next(r for r in await flow.rows("request") if r["message_id"] == original.id)
    assert request["state"] == "REQUESTED"
    await record_late_response(store, claim, code="DECIDED", http=200, data=decision)
    await manual_host.host.scan_pending(limit=1)
    assert (await flow.rows("task"))[0]["status"] == "CANCELLED"
    request = next(r for r in await flow.rows("request") if r["message_id"] == original.id)
    assert request["state"] == state and request["response"]["data"] == decision
    assert request["due_at_ms"] is None
    assert request["response"]["late_response_record_id"]
    (stopped,) = await flow.confirmations()
    assert (stopped.operation_id, stopped.status) == (original.operation_id, "ABANDONED")


async def send_original(flow, message_id):
    requests = flow.task.capabilities.wms
    store = MessageStore(flow.factory, records=history, clock=requests.outbox.clock)
    claims = await store.claim(10, requests.outbox.targets)
    claim = next(c for c in claims if c.message_id == message_id)
    claim = replace(
        claim, request=HttpRequest("POST", "/decisions", body=claim.message.request_raw)
    )
    assert await store.before_send(claim, requests.outbox.targets[claim.message.target])
    return store, claim


async def record_late_response(store, claim, *, code, http, data):
    raw = json.dumps(
        dict(
            operation_id=claim.message.operation_id,
            code=code,
            timestamp=100,
            data=data,
        )
    ).encode()
    assert not await store.finish(
        claim,
        AttemptOutcome(
            "DONE",
            result=HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, http, (), raw),
            verdict=Verdict(VerdictKind.DELIVERED, response=raw),
        ),
    )


async def test_cancelled_task_records_late_original_prepare(manual_host):
    flow = manual_host
    await flow.issue()
    await flow.prepare()
    (prepare,) = await flow.rows("request")
    store, claim = await send_original(flow, prepare["message_id"])
    assert (
        await flow.deliver(flow.message("cancel", dict(task_id="T1", cancel_scope="TASK")))
    ).status_code == 202
    await record_late_response(store, claim, code="PREPARE_ACCEPTED", http=202, data={})
    await flow.host.scan_pending(limit=1)
    (task,) = await flow.rows("task")
    (request,) = await flow.rows("request")
    assert task["status"] == "CANCELLED" and task["plan_revision"] == 0
    assert request["operation_id"] == prepare["operation_id"]
    assert request["state"] == "CONSUMED" and request["decision_code"] == "ACCEPTED"
    assert request["response"]["late_response_record_id"]
