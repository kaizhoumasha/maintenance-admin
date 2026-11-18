import json
from contextlib import suppress
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import MetaData, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.clock import unix_ms
from app.core.config import get_settings
from app.core.identifiers import uuid7
from app.database import SCHEMA
from app.http_client import DeliveryState, FailureKind, HttpResult
from app.integrations import history
from app.integrations.wms_request import WmsRequests
from app.integrations.wms_request.operation import lookup
from app.models import IntegrationRecord, ReliableDeliveryAttempt, ReliableMessage
from app.reliable import ScopeRef, VerdictKind
from app.reliable.delivery import send_one
from app.reliable.lifecycle import EvidencePersistenceError, evidence_transaction
from app.reliable.store import AttemptOutcome, MessageStore
from app.reliable.testing import ScriptedHttpClient
from tests.helpers import read_history_body, update_message_bodies
from tests.migration_helpers import sqlite_migration_start, state_migration  # noqa: F401

OP = "example.decision.ask@v1"


async def registered(sessions, wms):
    identity = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session, lookup(OP).data_model(task_id="T", qty=1), operation_id=identity
        )
    return ref.message_id, identity


async def test_request_result_uses_original_message_identity(sessions, wms):
    mid, oid = await registered(sessions, wms)
    reader = WmsRequests()  # no configured target needed for reading
    async with sessions() as session:
        found = await reader.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert found.availability == "FOUND" and found.target == "wms-decisions"
        assert found.delivery_status == "PENDING" and found.response_state == "NONE"
        for op, identity in [("other.op@v1", oid), (OP, str(uuid7()))]:
            wrong = await reader.get_result(
                session, message_id=mid, operation=op, operation_id=identity
            )
            assert wrong.availability == "IDENTITY_MISMATCH" and wrong.target is None
        missing = await reader.get_result(
            session, message_id=mid + 1, operation=OP, operation_id=oid
        )
        assert missing.availability == "NOT_FOUND" and missing.delivery_status is None


async def test_cutover_reads_original_terminal_response_and_received_time(
    sessions, wms, monkeypatch
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    mid, oid = await registered(sessions, wms)
    received = datetime(2026, 11, 13, 1, 2, 3, 456000, tzinfo=UTC)
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        row.status = "DONE"
        row.last_http_status = 200
        row.last_verdict = "DELIVERED"
        row.terminal_response_wire = json.dumps(
            dict(operation_id=oid, code="DECIDED", timestamp=10, data={"ok": True})
        ).encode()
        row.terminal_response_at = received
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert (result.availability, result.delivery_status, result.response_state) == (
            "FOUND",
            "DONE",
            "VALID",
        )
        assert (result.http_status, result.response_code, result.response_data) == (
            200,
            "DECIDED",
            {"ok": True},
        )
        assert result.response_timestamp == 10
        assert result.response_received_at_ms == unix_ms(received)


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"operation":"other.op@v1"}'])
async def test_invalid_original_request_cannot_expose_a_response(sessions, wms, raw):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await update_message_bodies(
            session, update(ReliableMessage).where(ReliableMessage.id == mid), request_raw=raw
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.availability == "INVALID"
        assert result.response_state == "UNAVAILABLE" and result.response_data is None
        assert result.target is None and result.delivery_status is None


@pytest.mark.parametrize(
    "status,http,code,data,expected",
    [
        ("DONE", 200, "DECIDED", {"ok": True}, "VALID"),
        ("DONE", 202, "PREPARE_ACCEPTED", {}, "VALID"),
        (
            "DONE",
            202,
            "PREPARE_ACCEPTED",
            {"task_id": "PICK-001", "workline_code": "KT16"},
            "VALID",
        ),
        ("FAILED", 422, "REJECTED", {"reason_code": "INVALID_DATA"}, "VALID"),
        ("FAILED", 409, "CONFLICT", {"reason_code": "STATE_CONFLICT"}, "VALID"),
        ("SENDING", 503, "UNAVAILABLE", {}, "NONE"),
        ("PENDING", 429, "BUSY", {"retry_after_ms": 10}, "NONE"),
        ("DONE", 200, "RECORDED", {}, "INVALID"),
        ("DONE", 200, "DECIDED", {}, "VALID"),
        ("EXPIRED", None, None, {}, "NONE"),
    ],
)
async def test_delivery_terminal_is_not_business_completion(
    sessions, wms, status, http, code, data, expected
):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code=code, timestamp=10, data=data)).encode()
    terminal = status in {"DONE", "FAILED"}
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status=status,
            last_http_status=http,
            last_verdict="PEER_TERMINAL" if status == "FAILED" else "DELIVERED",
            fail_reason=code if status == "FAILED" else None,
            response_raw=raw if terminal else None,
            last_response_raw=None if terminal else raw,
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == expected
        assert result.delivery_terminal == (status in {"DONE", "FAILED", "EXPIRED"})
        assert result.response_data == (data if expected == "VALID" else None)
        assert not hasattr(result, "completed")
        if expected == "VALID":
            with pytest.raises(FrozenInstanceError):
                result.response_code = "OTHER"
            result.response_data["mutated"] = True
            again = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
            assert "mutated" not in again.response_data


async def test_missing_terminal_response_and_retention_never_mean_success(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await session.execute(
            update(ReliableMessage).where(ReliableMessage.id == mid).values(status="DONE")
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "UNAVAILABLE"
    async with sessions.begin() as session:
        await session.execute(delete(ReliableMessage).where(ReliableMessage.id == mid))
    async with sessions() as session:
        assert (
            await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        ).availability == "NOT_FOUND"


async def test_reads_do_not_flush_or_reuse_old_orm_values(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions() as session:
        row = await session.get(ReliableMessage, mid)
        row.status = "DONE"  # pending local change must not flush or replace stored projection
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.delivery_status == "PENDING" and row in session.dirty
        assert not session.new
        await session.rollback()
    async with sessions() as session:
        assert await session.scalar(select(ReliableMessage.status)) == "PENDING"


@pytest.mark.parametrize("code", ["RECORDED", "DUPLICATE"])
async def test_fact_receipt_is_a_protocol_response(sessions, wms, code):
    op = "example.fact.report@v1"
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(session, lookup(op).data_model(task_id="T", qty=1), operation_id=oid)
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == ref.message_id),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=json.dumps(
                dict(operation_id=oid, code=code, timestamp=1, data={})
            ).encode(),
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=ref.message_id, operation=op, operation_id=oid)
        assert r.response_state == "VALID" and r.response_code == code and r.response_data == {}


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "wrong"},
        {"timestamp": True},
        {"data": []},
        {"extra": "invalid"},
    ],
)
async def test_invalid_terminal_envelope_is_not_a_decision(sessions, wms, change):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code="DECIDED", timestamp=1, data={}) | change).encode()
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=raw,
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.response_state == "INVALID" and r.response_data is None


async def test_local_error_never_exposes_stale_response(sessions, wms):
    mid, oid = await registered(sessions, wms)
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="FAILED",
            fail_reason="LOCAL_ERROR",
            last_response_raw=b'{"code":"DECIDED"}',
        )
    async with sessions() as session:
        r = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert r.delivery_terminal and r.fail_reason == "LOCAL_ERROR"
        assert r.response_state == "NONE" and r.response_data is None


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
async def test_overflowed_terminal_response_is_invalid_and_keeps_original_bytes(
    sessions, wms, number
):
    mid, oid = await registered(sessions, wms)
    raw = json.dumps(dict(operation_id=oid, code="DECIDED", timestamp=1, data={})).encode()
    raw = raw.replace(b'"data": {}', b'"data": {"nested": [' + number.encode() + b"]}")
    async with sessions.begin() as session:
        await update_message_bodies(
            session,
            update(ReliableMessage).where(ReliableMessage.id == mid),
            status="DONE",
            last_verdict="DELIVERED",
            last_http_status=200,
            response_raw=raw,
        )
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "INVALID" and result.response_data is None
        assert (
            await read_history_body(
                session,
                await session.scalar(
                    select(ReliableMessage.terminal_response_record_id).where(
                        ReliableMessage.id == mid
                    )
                ),
            )
            == raw
        )


async def test_result_reader_uses_only_original_terminal_reference(sessions, wms):
    from app.integrations.history import RecordDraft, write_record

    mid, oid = await registered(sessions, wms)
    raw = json.dumps(
        dict(operation_id=oid, code="DECIDED", timestamp=10, data={"ok": True})
    ).encode()
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        reply = await write_record(
            session,
            draft=RecordDraft(
                exchange_id=row.exchange_id,
                record_kind="HTTP_RESPONSE_OBSERVED",
                event_key="test-response",
                direction="INBOUND",
                raw_body=raw,
            ),
        )
        row.status, row.last_verdict, row.last_http_status = "DONE", "DELIVERED", 200
        row.last_response_record_id = reply
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "UNAVAILABLE"
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, mid)
        row.terminal_response_record_id = reply
        row.request_raw, row.response_raw = b"obsolete", b"obsolete"
    async with sessions() as session:
        result = await wms.get_result(session, message_id=mid, operation=OP, operation_id=oid)
        assert result.response_state == "VALID" and result.response_data == {"ok": True}


@pytest.fixture(params=["sqlite", "postgres"])
async def stopped_sessions(request, sessions, postgres_tables):
    if request.param == "sqlite":
        yield sessions
    else:
        metadata = MetaData(schema=SCHEMA)
        ReliableMessage.__table__.to_metadata(metadata)
        async with postgres_tables(
            request.getfixturevalue("pg_url"), "wms_stopped", metadata
        ) as engine:
            yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize(
    "lease_state,legacy_attempt",
    [
        ("active", False),
        ("active", True),
        ("active_missing_wire", False),
        ("active_refusals", False),
        ("expired", False),
        ("expired", True),
        ("released", False),
        ("released", True),
        ("retry_claimed", False),
        ("retry_claimed", True),
        ("retry_gated", False),
        ("retry_missing_gated", False),
        ("retry_missing_gated", True),
        ("retry_missing_expired", True),
        ("retry_missing_released", True),
        ("retry_missing_middle", True),
        ("retry_not_sent", False),
        ("retry_declined", False),
        ("retry_declined", True),
        ("retry_both_declined", False),
    ],
)
async def test_cancelled_scope_can_read_only_its_original_attempt_reply(
    stopped_sessions, wms, clock, monkeypatch, runtime_authority, lease_state, legacy_attempt
):
    sessions = stopped_sessions
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    scope = ScopeRef("owner", "TASK", "original")
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session, lookup(OP).data_model(task_id="T", qty=1), operation_id=oid, scope=scope
        )
    identity = dict(message_id=ref.message_id, operation=OP, operation_id=oid)
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (claim,) = await store.claim(1, outbox.targets)
    target = outbox.targets[claim.message.target]
    adapter = outbox.adapters[target.protocol]
    claim = replace(claim, request=adapter.build_request(claim.message, target))
    assert await store.before_send(claim, target)
    if lease_state == "active_refusals":
        raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
        response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
        verdict = adapter.classify_response(claim.message, response)
        assert verdict.kind == VerdictKind.NOT_RECEIVED

        async def finish_refusal(current):
            assert await store.finish(
                current,
                AttemptOutcome(
                    "PENDING", result=response, verdict=verdict, next_attempt_at=clock()
                ),
            )

        await finish_refusal(claim)
        clock.advance(40)
        (retry,) = await store.claim(1, outbox.targets)
        assert await store.before_send(
            replace(retry, request=adapter.build_request(retry.message, target)), target
        )
        await finish_refusal(retry)
        async with sessions.begin() as session:
            await outbox.stop_scope(session, scope, reason="TASK_CANCELLED")
        async with sessions() as session:
            result = await wms.get_result(session, **identity, stopped_reason="TASK_CANCELLED")
            assert result.delivery_status == "ABANDONED"
            assert not result.awaiting_stopped_response
            assert not result.delivery_uncertain and result.response_state == "NONE"
        return
    if lease_state in ("released", "retry_missing_released"):
        if lease_state == "released":
            clock.advance(1)
        assert await store.release(claim, outbox.targets)
    elif lease_state not in ("active", "active_missing_wire"):
        clock.advance(40)
        assert await store.recover_expired(1, outbox.targets) == 1
    if lease_state.startswith("retry_"):
        if lease_state != "retry_missing_released":
            clock.advance(40)
        (retry,) = await store.claim(1, outbox.targets)
        retry = replace(retry, request=adapter.build_request(retry.message, target))
        if lease_state != "retry_claimed":
            assert await store.before_send(retry, target)
        if lease_state in ("retry_missing_expired", "retry_missing_middle"):
            clock.advance(40)
            assert await store.recover_expired(1, outbox.targets) == 1
        if lease_state == "retry_missing_middle":
            clock.advance(40)
            (latest,) = await store.claim(1, outbox.targets)
            latest = replace(latest, request=adapter.build_request(latest.message, target))
            assert await store.before_send(latest, target)
            clock.advance(1)
    attempt_key = (claim.message_id, claim.lease_id)
    async with sessions.begin() as session:
        if legacy_attempt:
            attempt = await session.get(ReliableDeliveryAttempt, attempt_key)
            attempt.peer_endpoint = claim.message.target
            message = await session.get(ReliableMessage, claim.message_id)
            message.dispatch_attempt_count = None
        await outbox.stop_scope(session, scope, reason="TASK_CANCELLED")
        if lease_state == "active_missing_wire":
            attempt = await session.get(ReliableDeliveryAttempt, attempt_key)
            attempt.request_wire = None
        if lease_state.startswith("retry_missing_"):
            await session.delete(
                await session.get(ReliableDeliveryAttempt, (ref.message_id, retry.lease_id))
            )
        waiting = await wms.get_result(session, **identity, stopped_reason="TASK_CANCELLED")
        assert waiting.delivery_status == "ABANDONED" and waiting.awaiting_stopped_response
    if lease_state == "retry_not_sent":
        assert not await store.finish(
            retry,
            AttemptOutcome(
                "PENDING",
                next_attempt_at=clock(),
                result=HttpResult(DeliveryState.NOT_SENT, "CONNECT_ERROR", None, (), None),
            ),
        )
    if lease_state in ("retry_declined", "retry_both_declined", "retry_missing_middle"):
        refused_claim = claim if lease_state == "retry_missing_middle" else retry
        declined = HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            503,
            (),
            json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode(),
        )
        verdict = adapter.classify_response(refused_claim.message, declined)
        assert verdict.kind == VerdictKind.NOT_RECEIVED
        assert not await store.finish(
            refused_claim,
            AttemptOutcome("PENDING", result=declined, verdict=verdict, next_attempt_at=clock()),
        )
        if lease_state == "retry_both_declined":
            assert not await store.finish(
                claim,
                AttemptOutcome(
                    "PENDING", result=declined, verdict=verdict, next_attempt_at=clock()
                ),
            )
            async with sessions() as session:
                observed = await wms.get_result(
                    session, **identity, stopped_reason="TASK_CANCELLED"
                )
                original = await session.get(
                    ReliableDeliveryAttempt, (ref.message_id, retry.lease_id)
                )
                assert not observed.delivery_uncertain
                assert not observed.awaiting_stopped_response
                assert observed.response_state == "NONE"
                assert observed.late_response_record_id == str(original.response_record_id)
            return
        if not runtime_authority:
            async with sessions.begin() as session:
                refused = await session.get(
                    ReliableDeliveryAttempt, (ref.message_id, refused_claim.lease_id)
                )
                decline_id = refused.response_record_id
                record = await session.get(IntegrationRecord, decline_id)
                assert (
                    record.protocol_verdict == "NOT_RECEIVED" and record.raw_body == declined.body
                )
                # Older writers kept the verdict only in the HTTP response record.
                refused.response_verdict = None
    raw = json.dumps(
        dict(operation_id=oid, code="DECIDED", timestamp=10, data={"ok": True})
    ).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), raw)
    replied = latest if lease_state == "retry_missing_middle" else claim
    verdict = adapter.classify_response(replied.message, response)
    assert verdict.kind == VerdictKind.DELIVERED
    assert not await store.finish(replied, AttemptOutcome("DONE", result=response, verdict=verdict))
    async with sessions() as session:
        ordinary = await wms.get_result(session, **identity)
        unrelated = await wms.get_result(session, **identity, stopped_reason="TASK_COMPLETED")
        observed = await wms.get_result(session, **identity, stopped_reason="TASK_CANCELLED")
        for current in (ordinary, unrelated):
            assert current.delivery_status == "ABANDONED" and current.response_state == "NONE"
        assert observed.delivery_status == "ABANDONED"
        if (
            lease_state == "retry_gated"
            or lease_state.startswith("retry_missing_")
            or lease_state == "active_missing_wire"
            and runtime_authority
        ):
            assert observed.awaiting_stopped_response and observed.late_response_record_id is None
            assert observed.delivery_uncertain and observed.response_state == "NONE"
            return
        assert not observed.awaiting_stopped_response
        assert observed.response_state == "VALID" and observed.response_data == {"ok": True}
        assert observed.late_response_record_id and observed.response_received_at_ms == unix_ms(
            clock()
        )
    if lease_state == "retry_declined" and not runtime_authority:
        foreign_body = declined.body.replace(oid.encode(), str(uuid7()).encode())
        async with sessions() as session:
            for invalid in (
                {"attempt_id": claim.lease_id},
                {"raw_body": foreign_body},
                {"raw_body": foreign_body, "body_sha256": sha256(foreign_body).hexdigest()},
            ):
                savepoint = await session.begin_nested()
                await session.execute(
                    update(IntegrationRecord)
                    .where(IntegrationRecord.id == decline_id)
                    .values(**invalid)
                )
                uncertain = await wms.get_result(
                    session, **identity, stopped_reason="TASK_CANCELLED"
                )
                assert (
                    uncertain.awaiting_stopped_response
                    and uncertain.late_response_record_id is None
                )
                await savepoint.rollback()
    async with sessions.begin() as session:
        attempt = await session.get(ReliableDeliveryAttempt, attempt_key)
        attempt.protocol = "wms-facts"
    async with sessions() as session:
        mismatched = await wms.get_result(session, **identity, stopped_reason="TASK_CANCELLED")
        assert mismatched.awaiting_stopped_response and mismatched.late_response_record_id is None


@pytest.mark.parametrize(
    "legacy_evidence,unstarted_recovery",
    [
        ("valid", None),
        ("no_counter", None),
        ("no_counter_runtime", None),
        ("no_opening", None),
        ("wrong_lease", None),
        ("bad_hash", None),
        ("wrong_operation", None),
        ("no_counter", "expired"),
        ("no_counter_runtime", "expired"),
        ("no_counter", "released"),
        ("no_opening", "released"),
        ("no_counter", "before_late_reply"),
        ("valid", "older_late_reply"),
        ("no_counter", "older_late_reply"),
        ("no_counter_runtime", "older_late_reply"),
    ],
)
async def test_legacy_wms_refusal_applies_retry_limit_after_lease_recovery(
    stopped_sessions, wms, clock, monkeypatch, legacy_evidence, unstarted_recovery
):
    runtime_authority = legacy_evidence == "no_counter_runtime"
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = stopped_sessions
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (original,) = await store.claim(1, outbox.targets)
    target = replace(
        outbox.targets[original.message.target],
        max_not_received_attempts=(2 if unstarted_recovery == "older_late_reply" else 1),
    )
    targets = {target.name: target}
    adapter = outbox.adapters[target.protocol]
    original = replace(original, request=adapter.build_request(original.message, target))
    assert await store.before_send(original, target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    clock.advance(40)
    (second,) = await store.claim(1, targets)
    second = replace(second, request=adapter.build_request(second.message, target))
    assert await store.before_send(second, target)
    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    verdict = adapter.classify_response(second.message, response)
    assert verdict.kind == VerdictKind.NOT_RECEIVED
    decline = AttemptOutcome(
        "PENDING",
        result=response,
        verdict=verdict,
        next_attempt_at=clock(),
        not_received_count=1,
        retry_budget=1,
    )
    assert await store.finish(second, decline)
    async with sessions.begin() as session:
        attempt = await session.get(ReliableDeliveryAttempt, (ref.message_id, second.lease_id))
        if legacy_evidence in {"no_counter", "no_counter_runtime", "no_opening"}:
            message = await session.get(ReliableMessage, ref.message_id)
            message.dispatch_attempt_count = None
        if not runtime_authority:
            record = await session.get(IntegrationRecord, attempt.response_record_id)
            assert record.protocol_verdict == "NOT_RECEIVED"
            # Older writers kept the verdict only in the HTTP response record.
            attempt.response_verdict = None
            if legacy_evidence == "wrong_lease":
                record.attempt_id = original.lease_id
            if legacy_evidence in {"bad_hash", "wrong_operation"}:
                record.raw_body = raw.replace(oid.encode(), str(uuid7()).encode())
                if legacy_evidence == "wrong_operation":
                    record.body_sha256 = sha256(record.raw_body).hexdigest()
    if legacy_evidence == "no_opening":
        async with sessions.begin() as session:
            first_attempt = await session.get(
                ReliableDeliveryAttempt, (ref.message_id, original.lease_id)
            )
            opening = await session.get(IntegrationRecord, first_attempt.id)
            await session.delete(opening)

    async def recover_unstarted():
        clock.advance(1)
        (unstarted,) = await store.claim(1, targets)
        if unstarted_recovery == "released":
            assert await store.release(unstarted, targets)
        else:
            clock.advance(40)
            assert await store.recover_expired(1, targets) == 1
        clock.advance(40)

    if unstarted_recovery == "before_late_reply":
        await recover_unstarted()
    if unstarted_recovery == "older_late_reply":
        # The older sender stored the late response without incrementing the counter.
        async with sessions.begin() as session:
            await history.finished(session, claim=original, outcome=decline, at=clock(), late=True)
            if not runtime_authority:
                first_attempt = await session.get(
                    ReliableDeliveryAttempt, (ref.message_id, original.lease_id)
                )
                first_attempt.response_verdict = None
        await recover_unstarted()
    else:
        assert not await store.finish(original, decline)
        assert not await store.finish(original, decline)
    if unstarted_recovery in {"expired", "released"}:
        await recover_unstarted()

    clock.advance(1)
    restarted = MessageStore(sessions, clock=clock, records=history)
    client = ScriptedHttpClient(response)
    for third in await restarted.claim(1, targets):
        await send_one(
            third,
            store=restarted,
            targets=targets,
            adapters=outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )
    async with sessions() as session:
        message = await session.get(ReliableMessage, ref.message_id)
        if legacy_evidence in {"valid", "no_counter", "no_counter_runtime"}:
            assert not client.requests
            assert message.status == "STALLED"
            assert (message.dispatch_attempt_count, message.not_received_count) == (2, 2)
            assert not message.delivery_uncertain
        else:
            assert len(client.requests) == 1
            assert message.status == "PENDING" and message.delivery_uncertain


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("mixed_order", ["old_late", "old_gate"])
async def test_mixed_senders_reconcile_refusals_before_a_third_request(
    stopped_sessions, wms, clock, monkeypatch, runtime_authority, mixed_order
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = stopped_sessions
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (original,) = await store.claim(1, outbox.targets)
    target = replace(
        outbox.targets[original.message.target],
        max_not_received_attempts=2,
    )
    targets = {target.name: target}
    adapter = outbox.adapters[target.protocol]
    original = replace(original, request=adapter.build_request(original.message, target))
    assert await store.before_send(original, target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    clock.advance(40)
    (second,) = await store.claim(1, targets)
    second = replace(second, request=adapter.build_request(second.message, target))

    assert await store.before_send(second, target)
    if mixed_order == "old_gate":
        # The old gate writes the attempt but leaves the stored dispatch count untouched.
        async with sessions.begin() as session:
            row = await session.get(ReliableMessage, ref.message_id)
            row.dispatch_attempt_count = 1
    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    verdict = adapter.classify_response(second.message, response)
    assert verdict.kind == VerdictKind.NOT_RECEIVED
    decline = AttemptOutcome(
        "PENDING",
        result=response,
        verdict=verdict,
        next_attempt_at=clock(),
        not_received_count=1,
        retry_budget=2,
    )

    if mixed_order == "old_gate":
        assert not await store.finish(original, decline)
        assert await store.finish(second, decline)
    else:
        # The old settlement records an expired lease's response without counting it.
        async with sessions.begin() as session:
            assert await history.finished(
                session, claim=original, outcome=decline, at=clock(), late=True
            )
            if not runtime_authority:
                attempt = await session.get(
                    ReliableDeliveryAttempt, (ref.message_id, original.lease_id)
                )
                attempt.response_verdict = None
        assert await store.finish(second, decline)

    clock.advance(40)
    restarted = MessageStore(sessions, clock=clock, records=history)
    claims = await restarted.claim(1, targets)
    client = ScriptedHttpClient(response)
    for third in claims:
        await send_one(
            third,
            store=restarted,
            targets=targets,
            adapters=outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )
    assert not client.requests
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert (row.status, row.not_received_count, row.dispatch_attempt_count) == ("STALLED", 2, 2)
        assert not row.delivery_uncertain


settlement_schema = pytest.mark.parametrize(
    "state_migration",
    [
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "sqlite", "reliable_retry_budget_v1"),
        ("TEST_HISTORY_MIGRATION_DATABASE_URL", "postgresql", "reliable_retry_budget_v1"),
    ],
    indirect=True,
)


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("old_gate_count", [None, 1])
@settlement_schema
async def test_old_recovery_does_not_spend_a_third_wms_send(
    state_migration,  # noqa: F811
    wms,
    clock,
    monkeypatch,
    runtime_authority,
    old_gate_count,
):
    await state_migration.migrate("upgrade", "head")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (original,) = await store.claim(1, outbox.targets)
    target = replace(
        outbox.targets[original.message.target],
        max_not_received_attempts=2,
    )
    targets = {target.name: target}
    adapter = outbox.adapters[target.protocol]
    original = replace(original, request=adapter.build_request(original.message, target))
    assert await store.before_send(original, target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    clock.advance(40)
    (second,) = await store.claim(1, targets)
    second = replace(second, request=adapter.build_request(second.message, target))

    assert await store.before_send(second, target)
    async with sessions.begin() as session:
        # The older gate records the attempt without advancing this newer column.
        row = await session.get(ReliableMessage, ref.message_id)
        row.dispatch_attempt_count = old_gate_count
    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    verdict = adapter.classify_response(second.message, response)
    assert verdict.kind == VerdictKind.NOT_RECEIVED
    decline = AttemptOutcome(
        "PENDING",
        result=response,
        verdict=verdict,
        next_attempt_at=clock(),
        not_received_count=1,
        retry_budget=2,
    )

    assert not await store.finish(original, decline)
    assert await store.finish(second, decline)
    clock.advance(40)
    (unstarted,) = await store.claim(1, targets)
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert row.lease_not_started and row.attempts == 2
        assert row.dispatch_attempt_count == old_gate_count
    clock.advance(40)
    async with sessions.begin() as session:
        # The older recovery counted an expired lease even if it never passed the gate.
        row = await session.get(ReliableMessage, ref.message_id)
        row.status = "PENDING"
        row.lease_id = row.lease_until = None
        row.next_attempt_at = row.updated_at = clock()
        row.attempts += 1
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert row.attempts == 3 and row.dispatch_attempt_count == old_gate_count
        assert (
            await session.get(ReliableDeliveryAttempt, (ref.message_id, unstarted.lease_id)) is None
        )
    clock.advance(40)
    restarted = MessageStore(sessions, clock=clock, records=history)
    claims = await restarted.claim(1, targets)
    client = ScriptedHttpClient(response)
    for third in claims:
        await send_one(
            third,
            store=restarted,
            targets=targets,
            adapters=outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )
    assert not client.requests
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert (row.status, row.not_received_count, row.dispatch_attempt_count) == ("STALLED", 2, 2)
        assert not row.delivery_uncertain


@pytest.mark.parametrize("runtime_authority", [False, True])
async def test_resume_keeps_prior_refusals_out_of_the_new_budget(
    stopped_sessions, wms, clock, monkeypatch, runtime_authority
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = stopped_sessions
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (original,) = await store.claim(1, outbox.targets)
    target = replace(
        outbox.targets[original.message.target],
        max_not_received_attempts=2,
    )
    targets = {target.name: target}
    adapter = outbox.adapters[target.protocol]

    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    client = ScriptedHttpClient(response, response, response)

    async def send(claim):
        await send_one(
            claim,
            store=store,
            targets=targets,
            adapters=outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )

    await send(original)
    clock.advance(40)
    (second,) = await store.claim(1, targets)
    await send(second)
    async with sessions.begin() as session:
        assert await outbox.resume(session, target.name, second.message.operation, oid)

    (restarted_first,) = await store.claim(1, targets)
    restarted_first = replace(
        restarted_first, request=adapter.build_request(restarted_first.message, target)
    )
    assert await store.before_send(restarted_first, target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    verdict = adapter.classify_response(restarted_first.message, response)
    assert not await store.finish(
        restarted_first,
        AttemptOutcome(
            "PENDING",
            result=response,
            verdict=verdict,
            next_attempt_at=clock(),
            not_received_count=1,
            retry_budget=2,
        ),
    )
    clock.advance(40)
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert not row.delivery_uncertain
        count_after_first_refusal = row.not_received_count
    (second_after_reset,) = await store.claim(1, targets)
    await send(second_after_reset)
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert len(client.requests) == 3, (count_after_first_refusal, row.status)


class LegacyMessageStore(MessageStore):
    async def before_send(self, claim, target):
        # The old sender checks only the current lease and opens it without a budget check.
        async with evidence_transaction(self.session_factory) as session:
            row = await session.scalar(
                select(ReliableMessage)
                .where(ReliableMessage.id == claim.message_id)
                .with_for_update()
            )
            now = self.clock()
            if row.status != "SENDING" or row.lease_id != claim.lease_id:
                return None
            row.lease_not_started = False
            row.dispatch_started_at = row.dispatch_started_at or now
            row.lease_until = now + timedelta(
                seconds=target.timeout_ms / 1000 + self.lease_margin_s
            )
            await history.opened(
                session,
                claim=replace(
                    claim, dispatch_origin=target.base_url, dispatch_protocol=target.protocol
                ),
                at=now,
            )
            return row.lease_until

    async def finish(self, claim, outcome):
        # The old writer records an inactive refusal without settling the message budget.
        async with evidence_transaction(self.session_factory) as session:
            row = await session.scalar(
                select(ReliableMessage)
                .where(ReliableMessage.id == claim.message_id)
                .with_for_update()
            )
            active = row.status == "SENDING" and row.lease_id == claim.lease_id
            now = self.clock()
            await history.finished(session, claim=claim, outcome=outcome, at=now, late=not active)
            if active:
                await history.close_attempt(
                    session,
                    message_id=row.id,
                    lease_id=claim.lease_id,
                    at=now,
                    reason="FINISHED",
                    outcome=outcome,
                )
                row.status = "PENDING"
                row.attempts += 1
                row.not_received_count = claim.not_received_count + 1
                row.delivery_uncertain = claim.delivery_uncertain
                row.next_attempt_at = outcome.next_attempt_at
                row.updated_at = now
                row.lease_id = row.lease_until = None
            return active


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("missing_gate", [False, True])
@settlement_schema
async def test_legacy_gate_can_recover_original_after_two_refusals(
    state_migration,  # noqa: F811
    wms,
    clock,
    monkeypatch,
    runtime_authority,
    missing_gate,
):
    await state_migration.migrate("upgrade", "head")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    store = MessageStore(sessions, clock=clock, records=history)
    oid = str(uuid7())
    async with sessions.begin() as session:
        await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    target = replace(
        wms.outbox.targets["wms-decisions"], max_not_received_attempts=4 if missing_gate else 2
    )
    targets = {target.name: target}
    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    refusal = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    unknown = HttpResult(DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, None, (), None)
    first_three = (refusal, refusal, unknown) if missing_gate else (unknown, refusal, refusal)
    client = ScriptedHttpClient(*first_three, refusal)

    async def send(sender):
        (claimed,) = await sender.claim(1, targets)
        await send_one(
            claimed,
            store=sender,
            targets=targets,
            adapters=wms.outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )
        clock.advance(40)
        return claimed

    await send(store)
    await send(store)
    latest = await send(store)
    if missing_gate:
        async with sessions.begin() as session:
            row = await session.get(ReliableMessage, latest.message_id)
            row.dispatch_attempt_count = 2
            row.dispatch_retry_limit = 2
            attempt = await session.get(
                ReliableDeliveryAttempt, (latest.message_id, latest.lease_id)
            )
            await session.delete(attempt)
    await send(LegacyMessageStore(sessions, clock=clock, records=history))
    assert [json.loads(request.body)["operation_id"] for request in client.requests] == [oid] * 4


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize(
    ("reply_order", "old_writer"),
    [("late_first", None)]
    + [
        (order, writer)
        for order in ("old_claim_first", "old_claim_last")
        for writer in (
            None,
            "late",
            "second",
            "legacy_gates",
            "legacy_claim_count",
            "mixed_legacy_claim_count",
            "mismatched_refusal",
        )
    ],
)
@settlement_schema
async def test_legacy_resume_resets_the_budget_across_restart(
    state_migration,  # noqa: F811
    wms,
    clock,
    monkeypatch,
    runtime_authority,
    reply_order,
    old_writer,
):
    legacy_gates = old_writer in {
        "legacy_gates",
        "legacy_claim_count",
        "mixed_legacy_claim_count",
        "mismatched_refusal",
    }
    if legacy_gates:
        monkeypatch.setenv(
            "RELIABLE_TARGETS",
            json.dumps(
                {
                    "targets": {
                        "wms-decisions": {
                            "base_url": "http://wms.test",
                            "protocol": "wms-decisions",
                            "max_not_received_attempts": 2,
                        }
                    }
                }
            ),
        )
    await state_migration.migrate("upgrade", "head")
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    sessions = async_sessionmaker(state_migration.engine, expire_on_commit=False)
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (original,) = await store.claim(1, outbox.targets)
    target = replace(
        outbox.targets[original.message.target],
        max_not_received_attempts=2,
    )
    targets = {target.name: target}
    adapter = outbox.adapters[target.protocol]

    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    client = ScriptedHttpClient(*([response] * 5))

    async def send(claim, sender=None):
        await send_one(
            claim,
            store=sender or store,
            targets=targets,
            adapters=outbox.adapters,
            clients={target.name: client},
            clock=clock,
            uniform=lambda low, _: low,
        )

    await send(original)
    clock.advance(40)
    (second,) = await store.claim(1, targets)
    await send(second)
    async with sessions.begin() as session:
        # The older Outbox.resume statement updates only the message row.
        resumed = await session.scalar(
            update(ReliableMessage)
            .where(ReliableMessage.id == ref.message_id, ReliableMessage.status == "STALLED")
            .values(
                status="PENDING", not_received_count=0, next_attempt_at=clock(), updated_at=clock()
            )
            .returning(ReliableMessage.id)
        )
        assert resumed == ref.message_id
        if legacy_gates:
            # The pre-upgrade message had never passed a budget-aware gate.
            row = await session.get(ReliableMessage, ref.message_id)
            row.dispatch_retry_limit = None
            if old_writer in {"legacy_claim_count", "mixed_legacy_claim_count"}:
                # The previous release charged an unstarted lease before that fact was tracked.
                row.attempts += 1
                if runtime_authority:
                    # Once the archive is gone, recovery needs the durable unstarted witness.
                    row.undispatched_attempts += 1

    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    legacy = LegacyMessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    (restarted_first,) = await store.claim(1, targets)
    restarted_first = replace(
        restarted_first, request=adapter.build_request(restarted_first.message, target)
    )
    first_gate = legacy if legacy_gates and old_writer != "mixed_legacy_claim_count" else store
    assert await first_gate.before_send(restarted_first, target)
    assert await client.send(restarted_first.request) == response
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    verdict = adapter.classify_response(restarted_first.message, response)

    async def settle_late():
        writer = legacy if old_writer == "late" or legacy_gates else store
        assert not await writer.finish(
            restarted_first,
            AttemptOutcome(
                "PENDING",
                result=response,
                verdict=verdict,
                next_attempt_at=clock(),
                not_received_count=1,
                retry_budget=2,
            ),
        )

    if reply_order == "late_first":
        await settle_late()
    clock.advance(40)
    (second_after_reset,) = await store.claim(1, targets)
    await send(second_after_reset, legacy if old_writer == "second" or legacy_gates else store)
    clock.advance(40)
    queued = ()
    if reply_order == "old_claim_first":
        queued = await legacy.claim(1, targets)
        assert len(queued) == 1
    if reply_order != "late_first":
        await settle_late()
    if old_writer == "mismatched_refusal":
        foreign = raw.replace(oid.encode(), str(uuid7()).encode())
        async with sessions.begin() as session:
            attempt = await session.get(
                ReliableDeliveryAttempt, (ref.message_id, restarted_first.lease_id)
            )
            if runtime_authority:
                attempt.response_wire = foreign
            else:
                record = await session.get(IntegrationRecord, attempt.response_record_id)
                record.raw_body = foreign
                record.body_sha256 = sha256(foreign).hexdigest()
    if reply_order != "old_claim_first":
        queued = await legacy.claim(1, targets)
    for claim in queued:
        with suppress(EvidencePersistenceError):
            await send(claim, legacy)
    assert len(client.requests) == (5 if old_writer == "mismatched_refusal" else 4)
    if old_writer is None:
        async with sessions() as session:
            row = await session.get(ReliableMessage, ref.message_id)
            assert (row.status, row.not_received_count, row.delivery_uncertain) == (
                "STALLED",
                2,
                False,
            )


@pytest.mark.parametrize("old_gate_count, late_reply", [(1, False), (2, False), (2, True)])
@pytest.mark.parametrize("archived", [False, True])
async def test_runtime_recovery_keeps_uncertainty_when_an_attempt_is_missing(
    stopped_sessions,
    wms,
    clock,
    monkeypatch,
    old_gate_count,
    late_reply,
    archived,
):
    from sqlalchemy import delete, func, select

    settings = get_settings()
    monkeypatch.setattr(settings, "INTEGRATION_RUNTIME_AUTHORITY", not archived)
    sessions = stopped_sessions
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session,
            lookup(OP).data_model(task_id="T", qty=1),
            operation_id=oid,
            scope=ScopeRef("owner", "TASK", "original"),
        )
    outbox = wms.outbox
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    name = "wms-decisions"
    target = replace(outbox.targets[name], max_not_received_attempts=4)
    targets = {name: target}
    adapter = outbox.adapters[target.protocol]
    raw = json.dumps(dict(operation_id=oid, code="UNAVAILABLE", timestamp=10, data={})).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), raw)
    for _ in range(2):
        (claim,) = await store.claim(1, targets)
        await send_one(
            claim,
            store=store,
            targets=targets,
            adapters=outbox.adapters,
            clients={name: ScriptedHttpClient(response)},
            clock=clock,
            uniform=lambda low, _: low,
        )
        clock.advance(40)
    (possibly_delivered,) = await store.claim(1, targets)
    request = adapter.build_request(possibly_delivered.message, target)
    assert await store.before_send(replace(possibly_delivered, request=request), target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    clock.advance(40)
    async with sessions.begin() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert row.attempts == 3 and row.delivery_uncertain
        # Runtime-only attempts have no independent legacy witness.
        gates = await session.scalar(
            select(func.count())
            .select_from(IntegrationRecord)
            .where(
                IntegrationRecord.exchange_id == row.exchange_id,
                IntegrationRecord.record_kind == "SEND_ATTEMPT_OPENED",
            )
        )
        assert gates == (3 if archived else 0)
        attempts = list(
            await session.scalars(
                select(ReliableDeliveryAttempt).where(ReliableDeliveryAttempt.message_id == row.id)
            )
        )
        for attempt in attempts:
            attempt.request_wire = row.frozen_request_wire
            if attempt.lease_id != possibly_delivered.lease_id:
                attempt.response_wire = raw
                attempt.response_http_status = 503
                attempt.response_verdict = "NOT_RECEIVED"
        await session.flush()
        # Remove only the runtime attempt, retaining any legacy opening witness.
        await session.execute(
            delete(ReliableDeliveryAttempt).where(
                ReliableDeliveryAttempt.message_id == row.id,
                ReliableDeliveryAttempt.lease_id == possibly_delivered.lease_id,
            )
        )
        # The legacy sender did not update the new gate counter.
        row.dispatch_attempt_count = old_gate_count
    monkeypatch.setattr(settings, "INTEGRATION_RUNTIME_AUTHORITY", True)
    restarted = MessageStore(sessions, clock=clock, records=history)
    target = replace(target, max_not_received_attempts=2)
    decline = AttemptOutcome(
        "PENDING",
        result=response,
        verdict=adapter.classify_response(claim.message, response),
        next_attempt_at=clock(),
        not_received_count=1,
        retry_budget=2,
    )
    if late_reply:
        assert not await restarted.finish(claim, decline)
        async with sessions() as session:
            row = await session.get(ReliableMessage, ref.message_id)
            assert row.delivery_uncertain
    (new_claim,) = await restarted.claim(1, {name: target})
    assert await restarted.before_send(
        replace(new_claim, request=adapter.build_request(new_claim.message, target)), target
    )
    assert await restarted.finish(new_claim, decline)
    async with sessions() as session:
        row = await session.get(ReliableMessage, ref.message_id)
        assert row.delivery_uncertain, (row.status, row.dispatch_attempt_count)


@pytest.mark.parametrize("reply", ["UNAVAILABLE", "DECIDED"])
@pytest.mark.parametrize("stopped_lease", ["gated", "pre_upgrade", "unstarted"])
async def test_stopped_scope_retains_uncertainty_when_a_runtime_gate_is_missing(
    stopped_sessions, wms, clock, monkeypatch, reply, stopped_lease
):
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    sessions = stopped_sessions
    wms = WmsRequests(
        wms.outbox, clock=clock, owner_domain="owner", contract_consumers=lambda _: {"owner"}
    )
    scope = ScopeRef("owner", "TASK", "original")
    oid = str(uuid7())
    async with sessions.begin() as session:
        ref = await wms.send(
            session, lookup(OP).data_model(task_id="T", qty=1), operation_id=oid, scope=scope
        )
    store = MessageStore(sessions, clock=clock, records=history, uniform=lambda low, _: low)
    targets = wms.outbox.targets
    (first,) = await store.claim(1, targets)
    target = targets[first.message.target]
    adapter = wms.outbox.adapters[target.protocol]
    first = replace(first, request=adapter.build_request(first.message, target))
    assert await store.before_send(first, target)
    clock.advance(40)
    assert await store.recover_expired(1, targets) == 1
    clock.advance(40)

    legacy = LegacyMessageStore(sessions, clock=clock, records=history)
    (missing,) = await legacy.claim(1, targets)
    missing = replace(missing, request=adapter.build_request(missing.message, target))
    assert await legacy.before_send(missing, target)
    if stopped_lease == "unstarted":
        clock.advance(40)
        assert await store.recover_expired(1, targets) == 1
        clock.advance(40)
        assert len(await legacy.claim(1, targets)) == 1
    async with sessions.begin() as session:
        await wms.outbox.stop_scope(session, scope, reason="TASK_CANCELLED")
        state = await session.get(ReliableMessage, ref.message_id)
        assert state.delivery_uncertain
        expected = None if stopped_lease == "unstarted" else missing.lease_id
        assert state.scope_stopped_lease_id == expected
        if stopped_lease == "pre_upgrade":
            state.scope_stopped_lease_id = None
            state.delivery_uncertain = False
        assert (
            await session.scalar(
                select(IntegrationRecord.id).where(
                    IntegrationRecord.exchange_id == state.exchange_id,
                    IntegrationRecord.record_kind == "SEND_ATTEMPT_OPENED",
                )
            )
            is None
        )
        attempt = await session.get(ReliableDeliveryAttempt, (ref.message_id, missing.lease_id))
        assert attempt is not None
        await session.delete(attempt)

    decided = reply == "DECIDED"
    raw = json.dumps(dict(operation_id=oid, code=reply, timestamp=10, data={})).encode()
    response = HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200 if decided else 503, (), raw
    )
    verdict = adapter.classify_response(first.message, response)
    assert verdict.kind == (VerdictKind.DELIVERED if decided else VerdictKind.NOT_RECEIVED)
    restarted = MessageStore(sessions, clock=clock, records=history)
    assert not await restarted.finish(
        first,
        AttemptOutcome(
            "DONE" if decided else "PENDING",
            result=response,
            verdict=verdict,
            next_attempt_at=None if decided else clock(),
        ),
    )
    async with sessions() as session:
        observed = await wms.get_result(
            session,
            message_id=ref.message_id,
            operation=OP,
            operation_id=oid,
            stopped_reason="TASK_CANCELLED",
        )
        assert observed.delivery_uncertain
        assert observed.awaiting_stopped_response and observed.response_state == "NONE"
        if not decided:
            state = await session.get(ReliableMessage, ref.message_id)
            assert state.delivery_uncertain
