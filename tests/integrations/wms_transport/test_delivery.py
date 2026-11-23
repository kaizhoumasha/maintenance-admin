import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import delete, event, select

from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.http_client import (
    AsyncHttpClient,
    DeliveryState,
    FailureKind,
    HttpClientConfig,
    HttpResult,
)
from app.integrations import history as history_records
from app.integrations.wms_transport import Rack, RackPosition, TransportCaller
from app.integrations.wms_transport.acceptance import submission_observer
from app.models import ReliableDeliveryAttempt, ReliableMessage, TransportTask
from app.reliable import ScopeRef
from app.reliable.delivery import send_one
from app.reliable.sender import Sender
from app.reliable.store import MessageStore, cleanup_terminal
from app.reliable.testing import ScriptedHttpClient
from tests.helpers import message_history_view


def ack(raw, code):
    request = json.loads(raw)
    return json.dumps(
        {
            "operation_id": request["operation_id"],
            "code": code,
            "timestamp": request["timestamp"],
            "data": {"transport_task_id": request["data"]["transport_task_id"]},
        }
    ).encode()


def sender_for(rig, client, **kwargs):
    return Sender(
        MessageStore(rig.factory, clock=rig.clock, records=history_records),
        {rig.target.name: rig.target},
        rig.adapters,
        {rig.target.name: client},
        clock=rig.clock,
        **kwargs,
    )


async def deliver(sender):
    assert await sender.tick() == 1
    await asyncio.wait_for(asyncio.gather(*tuple(sender.inflight)), 5)


@pytest.mark.parametrize("transport_storage", ["pg_transport_rig"], indirect=True)
async def test_legacy_gate_cannot_reopen_interrupted_xray_entry(
    transport_storage, transport_request
):
    from app.reliable.lifecycle import EvidencePersistenceError, evidence_transaction

    rig = transport_storage
    rig.service.owner_domain = "xray"
    async with rig.factory.begin() as session:
        await rig.service.move_rack(session, **transport_request)

    store = MessageStore(rig.factory, clock=rig.clock, records=history_records)
    targets = {rig.target.name: rig.target}
    adapter = rig.adapters[rig.target.protocol]
    (claim,) = await store.claim(1, targets)
    until = await store.before_send(
        replace(claim, request=adapter.build_request(claim.message, rig.target)), rig.target
    )
    rig.clock.advance((until - rig.clock()).total_seconds())
    assert await store.recover_expired(1, targets) == 1
    rig.clock.advance(2)
    (retry,) = await store.claim(1, targets)
    assert retry.delivery_uncertain

    # The older sender committed this gate before making its HTTP request.
    with pytest.raises(EvidencePersistenceError):
        async with evidence_transaction(rig.factory) as session:
            row = await session.get(ReliableMessage, retry.message_id, with_for_update=True)
            assert row.lease_id == retry.lease_id
            row.lease_not_started = False
            await history_records.opened(
                session,
                claim=replace(
                    retry,
                    request=adapter.build_request(retry.message, rig.target),
                    dispatch_protocol=rig.target.protocol,
                    dispatch_origin=rig.target.base_url,
                ),
                at=rig.clock(),
            )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, retry.message_id)
        assert row.lease_not_started and row.delivery_uncertain


@pytest.mark.parametrize("transport_storage", ["pg_transport_rig"], indirect=True)
@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("old_gate", [False, True])
@pytest.mark.parametrize(
    "reply",
    [
        "foreign_task",
        "invalid_timestamp",
        "valid_extension",
        "duplicate_extension_key",
        "overflow",
        "foreign_request",
    ],
)
async def test_transport_retry_budget_requires_a_valid_original_refusal(
    transport_storage, transport_request, monkeypatch, runtime_authority, old_gate, reply
):
    from hashlib import sha256

    from app.models import IntegrationRecord
    from app.reliable.lifecycle import EvidencePersistenceError, evidence_transaction

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    async with rig.factory.begin() as session:
        await rig.service.move_rack(
            session, **transport_request, scope=ScopeRef("roller", "LINE", "original")
        )
    store = MessageStore(rig.factory, clock=rig.clock, records=history_records)
    (first,) = await store.claim(1, {rig.target.name: rig.target})
    response = ack(first.message.request_raw, "UNAVAILABLE")
    if reply == "valid_extension":
        body = json.loads(response)
        body["data"]["detail"] = ["\0", "\ud800", r"\u0000", "😀"]
        response = json.dumps(body).encode()[:-2] + b',"measurement":1e-4000}}'
    await send_one(
        first,
        store=store,
        targets={rig.target.name: rig.target},
        adapters=rig.adapters,
        clients={
            rig.target.name: ScriptedHttpClient(
                HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), response)
            )
        },
        clock=rig.clock,
        uniform=lambda low, _: low,
    )
    body = json.loads(response)
    if reply == "foreign_task":
        body["data"]["transport_task_id"] = "x" * len(first.message.business_key)
    elif reply == "invalid_timestamp":
        body["timestamp"] = True
    observed = json.dumps(body).encode()
    if reply == "duplicate_extension_key":
        observed = observed[:-2] + rb',"\u0000":1,"\u0000":2}}'
    elif reply == "overflow":
        observed = observed[:-2] + b',"measurement":1e400}}'
    async with rig.factory.begin() as session:
        stored = await session.get(ReliableMessage, first.message_id)
        stored.dispatch_retry_limit = 1
        stored.not_received_count = 0
        stored.delivery_uncertain = True
        attempt = await session.get(ReliableDeliveryAttempt, (first.message_id, first.lease_id))
        if reply == "foreign_request":
            attempt.request_wire = b'{"operation_id":"another-request"}'
        elif reply == "valid_extension":
            assert attempt.response_state == "RESPONSE_OBSERVED"
        elif runtime_authority:
            attempt.response_wire = observed
        else:
            record = await session.get(IntegrationRecord, attempt.response_record_id)
            record.raw_body = observed
            record.stored_bytes = len(observed)
            record.body_sha256 = sha256(observed).hexdigest()

    rig.clock.advance(40)
    target = replace(rig.target, max_not_received_attempts=1)
    (retry,) = await store.claim(1, {target.name: target})
    adapter = rig.adapters[target.protocol]
    retry = replace(retry, request=adapter.build_request(retry.message, target))

    async def open_legacy_gate():
        async with evidence_transaction(rig.factory) as session:
            stored = await session.get(ReliableMessage, retry.message_id, with_for_update=True)
            stored.lease_not_started = False
            await history_records.opened(
                session,
                claim=replace(
                    retry, dispatch_protocol=target.protocol, dispatch_origin=target.base_url
                ),
                at=rig.clock(),
            )

    budget_exhausted = reply == "valid_extension"
    if old_gate and budget_exhausted:
        with pytest.raises(EvidencePersistenceError) as failure:
            await open_legacy_gate()
        assert failure.value.__cause__.orig.sqlstate == "23514"
    elif old_gate:
        await open_legacy_gate()
    else:
        assert (await store.before_send(retry, target) is None) == budget_exhausted
    async with rig.factory() as session:
        attempt = await session.get(ReliableDeliveryAttempt, (retry.message_id, retry.lease_id))
        assert (attempt is None) == budget_exhausted


def pause_after_gate(sender, monkeypatch):
    opened = asyncio.get_running_loop().create_future()
    resume = asyncio.Event()
    gate = sender.store.before_send

    async def suspended(claim, target):
        until = await gate(claim, target)
        assert until is not None
        opened.set_result(until)
        await resume.wait()
        return until

    monkeypatch.setattr(sender.store, "before_send", suspended)
    return opened, resume


def pause_before_finish(sender, monkeypatch):
    evidence = asyncio.get_running_loop().create_future()
    resume = asyncio.Event()
    finish = sender.store.finish

    async def suspended(claim, outcome):
        evidence.set_result((claim, outcome))
        await resume.wait()
        return await finish(claim, outcome)

    monkeypatch.setattr(sender.store, "finish", suspended)
    return finish, evidence, resume


@pytest.mark.parametrize("previous", [None, "unknown", "unstarted"])
@pytest.mark.parametrize("deadline", ["lease", "ttl"])
@pytest.mark.parametrize("interruption", [None, "stop", "recover"])
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_skipped_send_gate_keeps_only_possible_dispatches(
    transport_storage,
    transport_request,
    monkeypatch,
    previous,
    deadline,
    interruption,
    runtime_authority,
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    scope = ScopeRef("roller", "LINE", "return")
    cycle_id = str(uuid7())
    async with rig.factory.begin() as session:
        handle = await rig.service.move_rack(
            session, **transport_request, scope=scope, cycle_id=cycle_id
        )
    client = ScriptedHttpClient(
        HttpResult(DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, None, (), None)
    )
    sender = sender_for(rig, client)
    try:
        if previous == "unknown":
            await deliver(sender)
            rig.clock.advance(1)
        elif previous == "unstarted":
            assert len(await sender.store.claim(1, sender.targets)) == 1
            rig.clock.advance(40)
            assert await sender.store.recover_expired(1, sender.targets) == 1
            rig.clock.advance(1)
        gate = sender.store.before_send

        async def delayed_gate(claim, target):
            until = await gate(claim, target)
            assert until is not None
            cutoff = claim.message.expires_at if deadline == "ttl" else until
            rig.clock.advance((cutoff - rig.clock()).total_seconds())
            if interruption == "stop":
                async with rig.factory.begin() as session:
                    (stopped,) = await rig.outbox.stop_scope(session, scope, reason="STOPPED")
                    assert stopped.dispatch == "MAY_HAVE_DISPATCHED"
            elif interruption == "recover":
                assert await sender.store.recover_expired(1, sender.targets) == 1
            return until

        monkeypatch.setattr(sender.store, "before_send", delayed_gate)
        await deliver(sender)
        previous_unknown = previous == "unknown"
        assert len(client.requests) == int(previous_unknown)
        async with rig.factory() as session:
            result = await rig.service.get_result(
                session,
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=transport_request["caller"],
            )
            assert result.submission_dispatch_started is previous_unknown
            assert result.submission_delivery_uncertain is previous_unknown
            if interruption == "stop":
                assert result.submission_delivery_status == "ABANDONED"
            if interruption == "stop" or deadline == "ttl":
                predecessors = await rig.service.list_unclosed_predecessors(
                    session,
                    caller=transport_request["caller"],
                    cycle_id=cycle_id,
                    predecessor_ids=(handle.transport_task_id,),
                    rack_id=transport_request["rack_id"],
                )
                assert bool(predecessors.unresolved) is previous_unknown
                if not previous_unknown:
                    assert predecessors.items[0].reason == "SUBMIT_NEVER_DISPATCHED"
            attempts = list(
                await session.scalars(
                    select(ReliableDeliveryAttempt.attempt_state)
                    .where(ReliableDeliveryAttempt.message_id == result.submission_message_id)
                    .order_by(ReliableDeliveryAttempt.started_at)
                )
            )
            assert attempts == (
                ["DELIVERY_UNKNOWN", "NOT_SENT"] if previous_unknown else ["NOT_SENT"]
            )
    finally:
        await sender.aclose()


@pytest.mark.parametrize(
    "settlement", ["new_only_claimed", "older_first", "newer_first", "missing_older_attempt"]
)
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_unsent_evidence_closes_predecessor_in_either_lease_order(
    transport_storage, transport_request, monkeypatch, settlement, runtime_authority
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    scope = ScopeRef("roller", "LINE", "return")
    cycle_id = str(uuid7())
    async with rig.factory.begin() as session:
        handle = await rig.service.move_rack(
            session, **transport_request, scope=scope, cycle_id=cycle_id
        )
    old_client, new_client = ScriptedHttpClient(), ScriptedHttpClient()
    older, newer = sender_for(rig, old_client), sender_for(rig, new_client)

    old_open, old_resume = pause_after_gate(older, monkeypatch)
    new_open, new_resume = pause_after_gate(newer, monkeypatch)
    try:
        assert await older.tick() == 1
        (old_task,) = older.inflight
        old_claim = older.inflight[old_task]
        old_until = await asyncio.wait_for(old_open, 5)
        rig.clock.advance((old_until - rig.clock()).total_seconds())
        assert await newer.store.recover_expired(1, newer.targets) == 1
        rig.clock.advance(1)
        if settlement == "new_only_claimed":
            (new_claim,) = await newer.store.claim(1, newer.targets)
            old_resume.set()
            await asyncio.wait_for(old_task, 5)
            async with rig.factory() as session:
                row = await session.get(ReliableMessage, new_claim.message_id)
                assert (row.status, row.lease_id) == ("SENDING", new_claim.lease_id)
            async with rig.factory.begin() as session:
                (stop,) = await rig.outbox.stop_scope(session, scope, reason="STOPPED")
                assert stop.dispatch == "NEVER_DISPATCHED"
        else:
            assert await newer.tick() == 1
            (new_task,) = newer.inflight
            new_until = await asyncio.wait_for(new_open, 5)
            rig.clock.advance((new_until - rig.clock()).total_seconds())
            async with rig.factory.begin() as session:
                (stop,) = await rig.outbox.stop_scope(session, scope, reason="STOPPED")
                assert stop.dispatch == "MAY_HAVE_DISPATCHED"
            completions = [(old_resume, old_task), (new_resume, new_task)]
            if settlement == "newer_first":
                completions.reverse()
            for resume, task in completions:
                resume.set()
                await asyncio.wait_for(task, 5)
                if task is old_task and settlement == "missing_older_attempt":
                    async with rig.factory.begin() as session:
                        await session.execute(
                            delete(ReliableDeliveryAttempt).where(
                                ReliableDeliveryAttempt.message_id == old_claim.message_id,
                                ReliableDeliveryAttempt.lease_id == old_claim.lease_id,
                            )
                        )
        assert not old_client.requests and not new_client.requests
        async with rig.factory() as session:
            result = await rig.service.get_result(
                session,
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=transport_request["caller"],
            )
            assert result.submission_delivery_status == "ABANDONED"
            missing = settlement == "missing_older_attempt"
            assert result.submission_dispatch_started is missing
            assert result.submission_delivery_uncertain is missing
            predecessors = await rig.service.list_unclosed_predecessors(
                session,
                caller=transport_request["caller"],
                cycle_id=cycle_id,
                predecessor_ids=(handle.transport_task_id,),
                rack_id=transport_request["rack_id"],
            )
            assert predecessors.complete is not missing
            assert bool(predecessors.unresolved) is missing
            if not missing:
                assert predecessors.items[0].reason == "SUBMIT_NEVER_DISPATCHED"
    finally:
        old_resume.set()
        new_resume.set()
        await older.aclose()
        await newer.aclose()


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize(
    "settlement",
    [
        "older_first",
        "newer_first",
        "third_send",
        "third_restart",
        "third_restart_old_unlimited",
        "third_release_old_unlimited",
    ],
)
@pytest.mark.parametrize("peer_declined", [False, True])
async def test_confirmed_non_delivery_applies_budget_after_lease_recovery(
    transport_storage, transport_request, monkeypatch, runtime_authority, settlement, peer_declined
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    rig.target = replace(rig.target, max_not_received_attempts=1)
    rig.outbox.targets = {rig.target.name: rig.target}
    async with rig.factory.begin() as session:
        await rig.service.move_rack(
            session, **transport_request, scope=ScopeRef("roller", "LINE", "return")
        )
    old_client = ScriptedHttpClient()
    older = sender_for(rig, old_client)
    if settlement == "third_send" or settlement.endswith("old_unlimited"):
        older.targets[rig.target.name] = replace(rig.target, max_not_received_attempts=None)
    opened, resume = pause_after_gate(older, monkeypatch)
    newer = None
    try:
        assert await older.tick() == 1
        (older_task,) = older.inflight
        until = await asyncio.wait_for(opened, 5)
        rig.clock.advance((until - rig.clock()).total_seconds())
        assert await older.store.recover_expired(1, older.targets) == 1
        rig.clock.advance(1)
        (claim,) = await older.store.claim(1, older.targets)
        assert claim.delivery_uncertain
        reply = (
            HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE,
                None,
                503,
                (),
                ack(claim.message.request_raw, "UNAVAILABLE"),
            )
            if peer_declined
            else HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
        )
        newer = sender_for(rig, ScriptedHttpClient(reply))
        if settlement == "older_first":
            resume.set()
            await asyncio.wait_for(older_task, 5)
        await send_one(
            claim,
            store=newer.store,
            targets=newer.targets,
            adapters=newer.adapters,
            clients=newer.clients,
            clock=rig.clock,
            uniform=lambda low, high: high,
        )
        third = None
        if settlement.startswith("third_"):
            rig.clock.advance(1)
            (third,) = await newer.store.claim(1, newer.targets)
            assert third.delivery_uncertain and third.not_received_count == 1
        if settlement != "older_first":
            resume.set()
            await asyncio.wait_for(older_task, 5)
        if settlement == "newer_first":
            rig.clock.advance(1)
            assert await newer.tick() == 0
        elif settlement == "third_send":
            client = ScriptedHttpClient(
                HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
            )
            await send_one(
                third,
                store=newer.store,
                targets=newer.targets,
                adapters=newer.adapters,
                clients={rig.target.name: client},
                clock=rig.clock,
                uniform=lambda low, high: high,
            )
            assert not client.requests
        elif settlement.startswith("third_restart"):
            rig.clock.advance((third.lease_until - rig.clock()).total_seconds())
            client = ScriptedHttpClient()
            restarted = sender_for(rig, client)
            try:
                assert await restarted.store.recover_expired(1, restarted.targets) == 0
                rig.clock.advance(1)
                assert await restarted.tick() == 0
                assert not client.requests
            finally:
                await restarted.aclose()
        elif settlement == "third_release_old_unlimited":
            assert not await newer.store.release(third, newer.targets)
            rig.clock.advance(1)
            assert await newer.tick() == 0
        assert not old_client.requests
        assert len(newer.clients[rig.target.name].requests) == 1
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, claim.message_id)
            assert (row.status, row.delivery_uncertain) == ("STALLED", False)
            assert row.not_received_count >= rig.target.max_not_received_attempts
            assert row.next_attempt_at is None
            assert row.lease_id is None and row.attempts == 2
            assert (row.dispatch_started_at is not None) is peer_declined
        rig.clock.advance(1)
        assert not await newer.store.claim(1, newer.targets)
    finally:
        resume.set()
        await older.aclose()
        if newer is not None:
            await newer.aclose()


@pytest.mark.parametrize("older_first", [False, True])
@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_late_peer_decline_counts_once_in_current_retry_budget(
    transport_storage, transport_request, monkeypatch, older_first, runtime_authority
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    rig.target = replace(rig.target, max_not_received_attempts=2)
    rig.outbox.targets = {rig.target.name: rig.target}
    async with rig.factory.begin() as session:
        await rig.service.move_rack(
            session, **transport_request, scope=ScopeRef("roller", "LINE", "return")
        )
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
        raw = message.frozen_request_wire
    older_client = ScriptedHttpClient(
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), ack(raw, "UNAVAILABLE"))
    )
    newer_client = ScriptedHttpClient(
        HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
    )
    older = sender_for(rig, older_client)
    newer = sender_for(rig, newer_client)
    real_finish, evidence, resume = pause_before_finish(older, monkeypatch)
    try:
        assert await older.tick() == 1
        (older_task,) = older.inflight
        original, decline = await asyncio.wait_for(evidence, 5)
        rig.clock.advance(40)
        assert await older.store.recover_expired(1, older.targets) == 1
        rig.clock.advance(1)
        (claim,) = await newer.store.claim(1, newer.targets)
        if older_first:
            resume.set()
            await asyncio.wait_for(older_task, 5)
        await send_one(
            claim,
            store=newer.store,
            targets=newer.targets,
            adapters=newer.adapters,
            clients=newer.clients,
            clock=rig.clock,
            uniform=lambda low, high: high,
        )
        if not older_first:
            resume.set()
            await asyncio.wait_for(older_task, 5)
            rig.clock.advance(1)
            assert await newer.tick() == 0
        assert len(older_client.requests) == len(newer_client.requests) == 1
        assert not await real_finish(original, decline)
        async with rig.factory.begin() as session:
            row = await session.get(ReliableMessage, claim.message_id)
            assert (row.status, row.not_received_count, row.delivery_uncertain) == (
                "STALLED",
                2,
                False,
            )
            assert row.dispatch_attempt_count == 2 and row.next_attempt_at is None
            assert await rig.outbox.resume(session, row.target, row.operation, row.operation_id)
        assert not await real_finish(original, decline)
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, claim.message_id)
            assert (row.status, row.not_received_count) == ("PENDING", 0)
    finally:
        resume.set()
        await older.aclose()
        await newer.aclose()


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("lease_state", ["unclaimed", "claimed", "expired"])
@pytest.mark.parametrize(
    ("old_budget", "new_budget", "expected_status"),
    [(1, 2, "DONE"), (1, None, "DONE"), (None, 1, "STALLED")],
)
async def test_late_peer_decline_leaves_budget_decision_to_current_sender(
    transport_storage,
    transport_request,
    monkeypatch,
    runtime_authority,
    lease_state,
    old_budget,
    new_budget,
    expected_status,
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    rig = transport_storage
    rig.service.owner_domain = "roller"
    async with rig.factory.begin() as session:
        await rig.service.move_rack(
            session, **transport_request, scope=ScopeRef("roller", "LINE", "return")
        )
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
        )
        message_id, raw = message.id, message.frozen_request_wire
    older_client = ScriptedHttpClient(
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), ack(raw, "UNAVAILABLE"))
    )
    accepted = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), ack(raw, "RECEIVED"))
    newer_client = ScriptedHttpClient(accepted)
    older = sender_for(rig, older_client)
    older.targets[rig.target.name] = replace(rig.target, max_not_received_attempts=old_budget)
    newer = sender_for(rig, newer_client)
    current_target = replace(rig.target, max_not_received_attempts=new_budget)
    newer.targets[rig.target.name] = current_target
    real_finish, evidence, resume = pause_before_finish(older, monkeypatch)
    try:
        assert await older.tick() == 1
        (older_task,) = older.inflight
        original, decline = await asyncio.wait_for(evidence, 5)
        rig.clock.advance(40)
        assert await newer.store.recover_expired(1, newer.targets) == 1
        rig.clock.advance(1)
        claim = None
        if lease_state != "unclaimed":
            (claim,) = await newer.store.claim(1, newer.targets)
        resume.set()
        await asyncio.wait_for(older_task, 5)
        assert not await real_finish(original, decline)
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, message_id)
            assert row.not_received_count == 1
            if claim is not None:
                assert (row.status, row.lease_id) == ("SENDING", claim.lease_id)
            else:
                assert row.status == "PENDING" and row.next_attempt_at is not None

        if lease_state == "expired":
            rig.clock.advance((claim.lease_until - rig.clock()).total_seconds())
            assert await older.store.recover_expired(1, older.targets) == 1
            async with rig.factory() as session:
                row = await session.get(ReliableMessage, message_id)
                assert (row.status, row.not_received_count, row.delivery_uncertain) == (
                    "PENDING",
                    1,
                    False,
                )
            rig.clock.advance(1)
            await newer.aclose()
            newer_client = ScriptedHttpClient(accepted)
            newer = sender_for(rig, newer_client)
            newer.targets[rig.target.name] = current_target
            claim = None
        if claim is not None:
            await send_one(
                claim,
                store=newer.store,
                targets=newer.targets,
                adapters=newer.adapters,
                clients=newer.clients,
                clock=rig.clock,
                uniform=lambda low, high: high,
            )
        else:
            await deliver(newer)
        if expected_status == "DONE":
            assert [request.body for request in newer_client.requests] == [raw]
        else:
            assert not newer_client.requests
        assert len(older_client.requests) == 1
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, message_id)
            assert (row.status, row.not_received_count, row.delivery_uncertain) == (
                expected_status,
                1,
                False,
            )
            assert row.lease_id is None
    finally:
        resume.set()
        await older.aclose()
        await newer.aclose()


async def test_scripted_ack_stops_delivery_of_frozen_request(transport_storage, transport_request):
    rig = transport_storage
    async with rig.factory() as session:
        handle = await rig.service.move_rack(session, **transport_request)
        await session.commit()
        message = await message_history_view(
            session,
            await session.scalar(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            ),
        )
        raw = message.request_raw
    client = ScriptedHttpClient(
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), ack(raw, "RECEIVED"))
    )
    sender = sender_for(rig, client)
    try:
        await deliver(sender)
        assert client.requests[0].body == raw
        assert await sender.tick() == 0
        async with rig.factory() as session:
            assert (
                await session.scalar(
                    select(ReliableMessage.status).where(ReliableMessage.target == rig.target.name)
                )
                == "DONE"
            )
        rig.clock.advance(91 * 86400)
        async with rig.factory() as session:
            await cleanup_terminal(session, now=rig.clock(), retention_days=90)
            await session.commit()
            assert await rig.service.move_rack(session, **transport_request) == handle
            await session.commit()
            assert (
                await session.scalar(
                    select(ReliableMessage.id).where(ReliableMessage.target == rig.target.name)
                )
                is None
            )
        assert await sender.tick() == 0
    finally:
        await sender.aclose()


@pytest.mark.parametrize("lose_response", [False, True])
async def test_real_http_preserves_bytes_and_recovers_lost_response(
    pg_transport_rig, transport_http_server, lose_response
):
    rig = pg_transport_rig

    async def peer(raw, writer, number):
        if lose_response and number == 1:
            writer.close()
            return
        code, status = ("DUPLICATE", 200) if lose_response else ("RECEIVED", 202)
        body = ack(raw, code)
        writer.write(f"HTTP/1.1 {status} OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()

    async with transport_http_server(peer) as (url, requests):
        rig.target = replace(rig.target, base_url=url)
        rig.outbox.targets = {rig.target.name: rig.target}
        async with rig.factory() as session:
            handle = await rig.service.move_rack(
                session,
                client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a1",
                caller=TransportCaller(rig.target.name, "station-local"),
                rack_id="货架",
                source=Rack("货架"),
                target=RackPosition("目标"),
                target_face=" 北 ",
            )
            await session.commit()
            message = await message_history_view(
                session,
                await session.scalar(
                    select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                ),
            )
            raw, operation_id = message.request_raw, message.operation_id

        def new_sender():
            client = AsyncHttpClient(HttpClientConfig(url, rig.target.name))
            return sender_for(rig, client)

        sender = new_sender()
        try:
            await deliver(sender)
            if lose_response:
                async with rig.factory() as session:
                    message = await message_history_view(
                        session,
                        await session.scalar(
                            select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                        ),
                    )
                    assert message.status == "PENDING" and message.delivery_uncertain
                    assert message.not_received_count == 0
                await sender.aclose()
                rig.clock.advance(1)
                sender = new_sender()
                await deliver(sender)
            async with rig.factory() as session:
                message = await message_history_view(
                    session,
                    await session.scalar(
                        select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
                    ),
                )
                assert message.status == "DONE" and message.request_raw == raw
                assert message.operation_id == operation_id
                assert (
                    json.loads(message.response_raw)["data"]["transport_task_id"]
                    == handle.transport_task_id
                )
            rig.clock.advance(3600)
            assert await sender.tick() == 0
            assert len(requests) == (2 if lose_response else 1)
            for headers, body in requests:
                assert headers.startswith(b"POST /api/v1/wes/transport-requests HTTP/1.1\r\n")
                assert b"content-type: application/json\r\n" in headers.lower()
                assert body == raw and b"station-local" not in body
        finally:
            await sender.aclose()


@pytest.mark.parametrize("fault", ["save", "settle_and_save", "finish"])
async def test_failed_evidence_or_finish_preserves_available_ack(
    transport_storage, transport_request, fault, rack_failure_callback
):
    rig = transport_storage
    rig.service.owner_domain = "original"
    async with rig.factory.begin() as session:
        handle = await rig.service.move_rack(session, **transport_request)
        message = await message_history_view(
            session,
            await session.scalar(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            ),
        )
    calls = 0
    notices = []

    class Peer:
        async def send(self, request):
            nonlocal calls
            calls += 1
            if fault == "settle_and_save":
                async with rig.factory.begin() as session:
                    await rack_failure_callback(rig, session, handle, "rack")
            return HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), ack(request.body, "RECEIVED")
            )

        async def aclose(self):
            pass

    def factory():
        session = rig.factory()

        def fail(_):
            raise ConnectionError("injected commit failure")

        if fault != "finish":
            event.listen(session.sync_session, "before_commit", fail)
        return session

    sender = sender_for(
        rig,
        Peer(),
        observe_delivered=submission_observer(rig.outbox.targets, factory, notices.append),
    )
    if fault == "finish":

        async def broken_finish(*args):
            raise ConnectionError("finish unavailable")

        sender.store.finish = broken_finish
    try:
        if fault == "finish":
            with pytest.raises(ConnectionError, match="finish unavailable"):
                await deliver(sender)
        else:
            await deliver(sender)
        async with rig.factory() as session:
            observed = await rig.service.get_submission_acceptance(
                session,
                transport_task_id=handle.transport_task_id,
                client_request_id=handle.client_request_id,
                caller=transport_request["caller"],
            )
            state = await message_history_view(
                session, await session.get(ReliableMessage, message.id)
            )
            task = await session.scalar(
                select(TransportTask).where(
                    TransportTask.transport_task_id == handle.transport_task_id
                )
            )
            assert (task.submission_acceptance is not None) is (fault == "finish")
            assert observed.state == ("MISSING" if fault == "settle_and_save" else "ACCEPTED")
            assert state.status == ("SENDING" if fault == "finish" else "DONE")
            assert (state.response_raw is not None) is (fault == "save")
        assert calls == 1 and notices == (["original"] if fault == "finish" else [])
    finally:
        await sender.aclose()
