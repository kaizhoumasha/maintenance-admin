"""确认未送达预算、未知事实保护与固定重提间隔(合同 §4.3;说明书 §5)。"""

import logging
from datetime import timedelta

import pytest

from app.http_client import DeliveryState, FailureKind, HttpResult
from app.models import ReliableMessage
from app.reliable.config import TargetConfig
from app.reliable.delivery import send_one
from app.reliable.policy import retry_delay_s
from app.reliable.store import AttemptOutcome, cleanup_terminal
from app.reliable.testing import ScriptedHttpClient
from app.reliable.types import MessageStatus, Verdict, VerdictKind

NOT_SENT = HttpResult(DeliveryState.NOT_SENT, FailureKind.CONNECT_ERROR, None, (), None)
UNKNOWN_DELIVERY = HttpResult(
    DeliveryState.DELIVERY_UNKNOWN, FailureKind.READ_TIMEOUT, None, (), None
)
COMPLETE = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 503, (), b"peer")
NOT_RECEIVED = (COMPLETE, Verdict(VerdictKind.NOT_RECEIVED))
UNCLASSIFIED = (COMPLETE, Verdict(VerdictKind.UNKNOWN))


def with_target(rig, **config):
    target = TargetConfig(rig.target.name, "http://example.test", "script", **config)
    rig.targets = {target.name: target}
    rig.outbox.targets = rig.targets
    return rig


async def enqueue(rig, **kwargs):
    async with rig.factory() as session:
        result = await rig.outbox.enqueue(session, rig.draft(**kwargs))
        await session.commit()
        return result.message_id


async def row(rig, message_id):
    async with rig.factory() as session:
        return await session.get(ReliableMessage, message_id, populate_existing=True)


async def attempt(rig, outcome, *, uniform=lambda low, high: high):
    """领取一次并按 outcome 结束;outcome 为 HttpResult 或 (HttpResult, Verdict)。"""
    result, verdict = outcome if isinstance(outcome, tuple) else (outcome, None)
    rig.adapter.verdict = verdict
    (claim,) = await rig.store.claim(1, rig.targets)
    await send_one(
        claim,
        store=rig.store,
        targets=rig.targets,
        adapters=rig.adapters,
        clients={rig.target.name: ScriptedHttpClient(result)},
        clock=rig.clock,
        uniform=uniform,
    )
    return claim


async def run(rig, outcomes):
    for outcome in outcomes:
        rig.clock.advance(400)
        await attempt(rig, outcome)


async def test_budget_stalls_after_n_definite_non_deliveries(storage, caplog):
    rig = with_target(storage, max_not_received_attempts=3)
    message_id = await enqueue(rig, ttl=timedelta(hours=1))
    original = (await row(rig, message_id)).request_raw
    caplog.set_level(logging.WARNING)
    await run(rig, [NOT_SENT, NOT_RECEIVED])
    pending = await row(rig, message_id)
    assert pending.status == "PENDING" and pending.not_received_count == 2
    await run(rig, [NOT_SENT])
    stalled = await row(rig, message_id)
    assert stalled.status == "STALLED"
    assert stalled.attempts == 3 and stalled.not_received_count == 3
    assert stalled.next_attempt_at is None and stalled.fail_reason is None
    assert stalled.delivery_uncertain is False
    assert stalled.request_raw == original and stalled.lease_id is None
    assert "reliable stalled" in caplog.text


async def test_stalled_is_not_claimed_expired_or_cleaned(storage):
    rig = with_target(storage, max_not_received_attempts=1)
    message_id = await enqueue(rig, ttl=timedelta(hours=1))
    await run(rig, [NOT_SENT])
    rig.clock.advance(400 * 86400)
    assert not await rig.store.claim(1, rig.targets)
    assert await rig.store.expire_pending(10) == 0
    assert await rig.store.recover_expired(10, rig.targets) == 0
    async with rig.factory() as session:
        assert await cleanup_terminal(session, now=rig.clock(), retention_days=1) == 0
        await session.commit()
    assert (await row(rig, message_id)).status == "STALLED"


@pytest.mark.parametrize("unknown", [UNKNOWN_DELIVERY, UNCLASSIFIED], ids=["delivery", "verdict"])
async def test_unknown_neither_counts_nor_resets_and_blocks_stalling(storage, unknown):
    rig = with_target(storage, max_not_received_attempts=3)
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT, unknown, NOT_RECEIVED])
    current = await row(rig, message_id)
    assert current.status == "PENDING" and current.not_received_count == 2
    assert current.delivery_uncertain is True
    await run(rig, [NOT_SENT] * 5)
    current = await row(rig, message_id)
    assert current.status == "PENDING" and current.not_received_count == 7
    assert current.delivery_uncertain is True and current.next_attempt_at is not None


async def test_unknown_marker_survives_later_certain_outcomes(storage):
    rig = with_target(storage, max_not_received_attempts=2)
    message_id = await enqueue(rig)
    await run(rig, [UNKNOWN_DELIVERY, NOT_SENT, NOT_RECEIVED])
    current = await row(rig, message_id)
    assert current.status == "PENDING" and current.delivery_uncertain is True


@pytest.mark.parametrize("path", ["recover", "release"])
async def test_lease_recovery_marks_delivery_uncertain(storage, path):
    rig = with_target(storage, max_not_received_attempts=2)
    message_id = await enqueue(rig)
    (claim,) = await rig.store.claim(1, rig.targets)
    if path == "recover":
        rig.clock.advance(30)
        assert await rig.store.recover_expired(1, rig.targets) == 1
    else:
        assert await rig.store.release(claim, rig.targets)
    recovered = await row(rig, message_id)
    assert recovered.status == "PENDING" and recovered.delivery_uncertain is True
    await run(rig, [NOT_SENT, NOT_SENT, NOT_SENT])
    assert (await row(rig, message_id)).status == "PENDING"


async def test_stale_writeback_cannot_change_budget_state(storage):
    rig = with_target(storage, max_not_received_attempts=1)
    message_id = await enqueue(rig)
    (old,) = await rig.store.claim(1, rig.targets)
    rig.clock.advance(30)
    assert await rig.store.recover_expired(1, rig.targets) == 1
    stale = AttemptOutcome(
        MessageStatus.STALLED, result=NOT_SENT, not_received_count=1, delivery_uncertain=False
    )
    assert not await rig.store.finish(old, stale)
    current = await row(rig, message_id)
    assert current.status == "PENDING" and current.delivery_uncertain is True
    assert current.not_received_count == 0


async def test_resume_returns_stalled_message_with_same_bytes(storage):
    rig = with_target(storage, max_not_received_attempts=2)
    message_id = await enqueue(rig)
    original = (await row(rig, message_id)).request_raw
    await run(rig, [NOT_SENT, NOT_SENT])
    assert (await row(rig, message_id)).status == "STALLED"
    async with rig.factory() as session:
        assert await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        await session.commit()
    resumed = await row(rig, message_id)
    assert resumed.status == "PENDING" and resumed.not_received_count == 0
    assert resumed.request_raw == original and resumed.delivery_uncertain is False
    assert resumed.next_attempt_at is not None
    assert (await rig.store.claim(1, rig.targets))[0].message_id == message_id
    async with rig.factory() as session:
        assert not await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        assert not await rig.outbox.resume(session, rig.target.name, "opaque", "missing")


async def test_resume_does_not_clear_unknown_marker(storage):
    rig = with_target(storage, max_not_received_attempts=2)
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT, NOT_SENT])
    async with rig.factory() as session:
        await session.execute(
            ReliableMessage.__table__.update()
            .where(ReliableMessage.id == message_id)
            .values(delivery_uncertain=True)
        )
        assert await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        await session.commit()
    assert (await row(rig, message_id)).delivery_uncertain is True


async def test_pending_message_is_not_resumed(storage):
    rig = with_target(storage, max_not_received_attempts=5)
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT])
    before = await row(rig, message_id)
    async with rig.factory() as session:
        assert not await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        await session.commit()
    after = await row(rig, message_id)
    assert (after.status, after.not_received_count, after.next_attempt_at) == (
        before.status,
        before.not_received_count,
        before.next_attempt_at,
    )


async def test_without_budget_behaviour_is_unchanged(storage):
    rig = storage
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT, NOT_RECEIVED] * 6)
    current = await row(rig, message_id)
    assert current.status == "PENDING" and current.attempts == 12
    assert current.delivery_uncertain is False and current.next_attempt_at is not None


def target(**config):
    return TargetConfig("peer", "http://example.test", "script", **config)


@pytest.mark.parametrize("attempt_no", range(1, 31))
def test_fixed_interval_ignores_attempt_number_and_randomness(attempt_no):
    fixed = target(fixed_retry_interval_s=30)
    assert retry_delay_s(attempt_no, fixed, None, lambda low, high: 0.0) == 30
    assert retry_delay_s(attempt_no, fixed, None, lambda low, high: high) == 30


def test_fixed_interval_keeps_retry_hint_rule_and_default_stays_randomised():
    fixed = target(fixed_retry_interval_s=30, max_retry_after_s=100)
    assert retry_delay_s(3, fixed, 10_000, lambda low, high: 0.0) == 30
    assert retry_delay_s(3, fixed, 60_000, lambda low, high: 0.0) == 60
    assert retry_delay_s(3, fixed, 600_000, lambda low, high: 0.0) == 100
    assert retry_delay_s(3, target(), None, lambda low, high: 1.5) == 1.5
    assert retry_delay_s(3, target(), None, lambda low, high: high) == 4


async def test_fixed_interval_is_shared_by_send_and_lease_recovery(storage):
    rig = with_target(storage, fixed_retry_interval_s=30)
    message_id = await enqueue(rig)
    started = rig.clock()
    await attempt(rig, NOT_SENT, uniform=lambda low, high: 0.0)
    sent = await row(rig, message_id)
    assert (sent.next_attempt_at.replace(tzinfo=started.tzinfo) - started).total_seconds() == 30
    rig.clock.advance(30)
    await rig.store.claim(1, rig.targets)
    rig.clock.advance(30)
    now = rig.clock()
    assert await rig.store.recover_expired(1, rig.targets) == 1
    recovered = await row(rig, message_id)
    assert (recovered.next_attempt_at.replace(tzinfo=now.tzinfo) - now).total_seconds() == 30


@pytest.mark.parametrize("commit", [True, False])
async def test_resume_is_logged_only_once_the_caller_commits(storage, caplog, commit):
    rig = with_target(storage, max_not_received_attempts=1)
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT])
    caplog.set_level(logging.INFO)
    async with rig.factory() as session:
        assert await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        assert "reliable resumed" not in caplog.text
        await (session.commit() if commit else session.rollback())
        # 会话在回滚后继续使用并提交,也不能补记日志。
        await session.commit()
    assert ("reliable resumed" in caplog.text) is commit
    if commit:
        assert "'operation_id': 'id'" in caplog.text
    assert (await row(rig, message_id)).status == ("PENDING" if commit else "STALLED")


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("nested_commit", [False, True])
async def test_resume_in_savepoint_is_logged_only_after_outer_commit(
    storage, caplog, commit, nested_commit
):
    rig = with_target(storage, max_not_received_attempts=1)
    message_id = await enqueue(rig)
    await run(rig, [NOT_SENT])
    caplog.set_level(logging.INFO)
    async with rig.factory() as session:
        await rig.outbox.enqueue(session, rig.draft("outer"))
        nested = await session.begin_nested()
        assert await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        await (nested.commit() if nested_commit else nested.rollback())
        assert "reliable resumed" not in caplog.text
        await (session.commit() if commit else session.rollback())
    assert ("reliable resumed" in caplog.text) is (commit and nested_commit)
    assert (await row(rig, message_id)).status == (
        "PENDING" if commit and nested_commit else "STALLED"
    )


async def test_savepoint_rollback_keeps_outer_resume_log(storage, caplog):
    rig = with_target(storage, max_not_received_attempts=1)
    await enqueue(rig)
    await run(rig, [NOT_SENT])
    caplog.set_level(logging.INFO)
    async with rig.factory() as session:
        assert await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        nested = await session.begin_nested()
        assert not await rig.outbox.resume(session, rig.target.name, "opaque", "id")
        await nested.rollback()
        await session.commit()
    assert "reliable resumed" in caplog.text
