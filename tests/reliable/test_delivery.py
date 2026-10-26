import asyncio
import logging
from datetime import timedelta

import pytest

from app.http_client import DeliveryState, FailureKind, HttpResult
from app.models import ReliableMessage


@pytest.mark.parametrize(
    "state,failure,kind,reason,status",
    [
        ("NOT_SENT", "CONNECT_ERROR", None, None, "PENDING"),
        ("DELIVERY_UNKNOWN", "READ_TIMEOUT", None, None, "PENDING"),
        ("HTTP_RESPONSE_COMPLETE", None, "UNKNOWN", None, "PENDING"),
        ("HTTP_RESPONSE_COMPLETE", None, "NOT_RECEIVED", None, "PENDING"),
        ("HTTP_RESPONSE_COMPLETE", None, "DELIVERED", None, "DONE"),
        *[
            ("HTTP_RESPONSE_COMPLETE", None, "PEER_TERMINAL", reason, "FAILED")
            for reason in ("CONFLICT", "REJECTED", "FAILURE", "CONTRACT_VIOLATION")
        ],
    ],
)
async def test_delivery_matrix_preserves_actual_evidence(
    rig, state, failure, kind, reason, status, caplog
):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient
    from app.reliable.types import FailReason, Verdict, VerdictKind

    caplog.set_level(logging.INFO)
    result = HttpResult(
        DeliveryState(state),
        FailureKind(failure) if failure else None,
        503 if kind else None,
        (),
        b"actual" if kind else None,
    )
    if kind:
        rig.adapter.verdict = Verdict(
            VerdictKind(kind),
            response=b"actual" if kind == "DELIVERED" else None,
            reason=FailReason(reason) if reason else None,
        )
    async with rig.factory() as session:
        enqueued = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    client = ScriptedHttpClient(result)
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={"peer": client},
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, enqueued.message_id)
        assert row.status == status and row.attempts == 1
        assert row.last_http_status == result.status_code
        assert row.last_failure_kind == failure
        final = status in ("DONE", "FAILED")
        assert row.last_response_raw == (None if final else result.body)
        assert row.response_raw == (b"actual" if final else None)
    assert len(client.requests) == 1
    assert client.requests[0].body == claim.message.request_raw
    assert len(rig.adapter.builds) == 2
    assert "actual" not in caplog.text
    assert not any(
        record.getMessage().startswith("reliable delivery failed ") for record in caplog.records
    )
    if reason == "CONTRACT_VIOLATION":
        assert any(record.levelno >= logging.ERROR for record in caplog.records)


async def test_hint_over_local_cap_and_env_limit_and_unlimited_attempts(rig, caplog):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient
    from app.reliable.types import Verdict, VerdictKind

    async with rig.factory() as session:
        enqueued = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(days=2)))
        await session.commit()
    original_expiry = rig.clock() + timedelta(days=2)
    requests = []
    for hint in [600000, 7200000] + [None] * 8:
        rig.adapter.verdict = Verdict(VerdictKind.NOT_RECEIVED, retry_after_ms=hint)
        client = ScriptedHttpClient(
            HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"wait")
        )
        (claim,) = await rig.store.claim(1, rig.targets)
        await send_one(
            claim,
            store=rig.store,
            targets=rig.targets,
            adapters=rig.adapters,
            clients={"peer": client},
            clock=rig.clock,
            uniform=lambda low, high: high,
        )
        requests.extend(client.requests)
        assert not await rig.store.claim(1, rig.targets)
        async with rig.factory() as session:
            row = await session.get(ReliableMessage, enqueued.message_id)
            delay = (
                row.next_attempt_at.replace(tzinfo=rig.clock().tzinfo) - rig.clock()
            ).total_seconds()
            if hint:
                assert delay == min(hint / 1000, 3600)
            assert row.status == "PENDING"
            assert row.expires_at.replace(tzinfo=rig.clock().tzinfo) == original_expiry
        rig.clock.advance(delay)
    assert "retry hint capped" in caplog.text
    assert "retry threshold" in caplog.text
    rig.adapter.verdict = None
    client = ScriptedHttpClient(
        HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"done")
    )
    (claim,) = await rig.store.claim(1, rig.targets)
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={"peer": client},
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    requests.extend(client.requests)
    assert len(requests) == 11 and all(request == requests[0] for request in requests)
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, enqueued.message_id)
        assert row.status == "DONE" and row.attempts == 11 and row.response_raw == b"done"


async def test_ttl_before_send_and_cancellation_and_save_failure(rig):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient

    async with rig.factory() as session:
        enqueued = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(seconds=1)))
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    rig.clock.advance(1)
    client = ScriptedHttpClient()
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={"peer": client},
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    assert not client.requests
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, enqueued.message_id)
        assert row.status == "EXPIRED" and row.attempts == 0
        await rig.outbox.enqueue(session, rig.draft("cancel"))
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    with pytest.raises(asyncio.CancelledError):
        await send_one(
            claim,
            store=rig.store,
            targets=rig.targets,
            adapters=rig.adapters,
            clients={"peer": ScriptedHttpClient(asyncio.CancelledError())},
            clock=rig.clock,
            uniform=lambda low, high: high,
        )
    async with rig.factory() as session:
        assert (await session.get(ReliableMessage, claim.message_id)).status == "SENDING"


async def test_local_error_isolated_and_current_config_revalidated(rig):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient

    async with rig.factory() as session:
        message = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    rig.adapter.changes["body"] = b"changed"
    client = ScriptedHttpClient()
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={"peer": client},
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    assert not client.requests
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message.message_id)
        assert row.status == "FAILED" and row.fail_reason == "LOCAL_ERROR"


async def test_classifier_error_keeps_http_evidence_without_terminal_response(rig):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient

    async with rig.factory() as session:
        message = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)

    def broken(message, result):
        raise RuntimeError("sensitive classifier text")

    rig.adapter.classify_response = broken
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={
            "peer": ScriptedHttpClient(
                HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"actual")
            )
        },
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message.message_id)
        assert row.status == "FAILED" and row.fail_reason == "LOCAL_ERROR"
        assert row.last_http_status == 200 and row.last_response_raw == b"actual"
        assert row.response_raw is None


async def test_started_request_may_complete_after_ttl(rig):
    from app.reliable.delivery import send_one
    from app.reliable.testing import FaultInjectingHttpClient, ScriptedHttpClient

    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(seconds=1)))
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)

    async def after(request, response):
        rig.clock.advance(1)

    client = FaultInjectingHttpClient(
        ScriptedHttpClient(HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")),
        after=after,
    )
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={"peer": client},
        clock=rig.clock,
        uniform=lambda low, high: high,
    )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, result.message_id)
        assert row.status == "DONE" and row.response_raw == b"ok"


@pytest.mark.parametrize(
    "phase", ["prepare", "build_request", "send", "classify_response", "apply_outcome"]
)
async def test_local_failure_reports_safe_phase_without_changing_evidence(rig, caplog, phase):
    from app.reliable.delivery import send_one
    from app.reliable.testing import ScriptedHttpClient

    async with rig.factory.begin() as session:
        message = await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, rig.targets)
    error = RuntimeError("private_sql private_token http://user:private_password@host private_body")

    def fail(*args):
        raise error

    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"private_body")
    client = ScriptedHttpClient(error if phase == "send" else response)

    def uniform(low, high):
        return high

    if phase == "prepare":
        rig.adapters.clear()
    elif phase == "build_request":
        rig.adapter.error = error
    elif phase == "classify_response":
        rig.adapter.classify_response = fail
    elif phase == "apply_outcome":
        client = ScriptedHttpClient(HttpResult(DeliveryState.NOT_SENT, None, None, (), None))
        uniform = fail
    with caplog.at_level(logging.ERROR):
        await send_one(
            claim,
            store=rig.store,
            targets=rig.targets,
            adapters=rig.adapters,
            clients={"peer": client},
            clock=rig.clock,
            uniform=uniform,
        )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message.message_id)
        assert row.status == "FAILED" and row.fail_reason == "LOCAL_ERROR"
        assert row.response_raw is None
        assert row.last_response_raw == (b"private_body" if phase == "classify_response" else None)
    records = [r for r in caplog.records if r.getMessage().startswith("reliable delivery failed ")]
    assert len(records) == 1
    record = records[0]
    assert record.component == "reliable" and record.phase == phase
    assert record.message_id == claim.message_id and record.target == "peer"
    assert "origin=app/" in record.getMessage() and "Traceback" in record.getMessage()
    assert "private_" not in caplog.text
