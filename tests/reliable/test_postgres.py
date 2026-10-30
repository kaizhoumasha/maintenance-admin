import asyncio
import json
import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import Insert

from app.http_client import DeliveryState, HttpResult
from app.models import ReliableMessage
from app.reliable import DuplicateMessage
from app.reliable.delivery import send_one
from app.reliable.store import MessageStore


async def wait_for_database_lock(factory, pending):
    async with factory() as session, asyncio.timeout(5):
        while not pending.done():
            if await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock')"
                )
            ):
                return
            await session.rollback()
    assert not pending.done()


@pytest.mark.parametrize("rollback", [False, True])
async def test_concurrent_first_enqueue_preserves_outer_transactions(pg_rig, rollback):
    rig = pg_rig
    async with rig.factory() as first:
        original = await rig.outbox.enqueue(first, rig.draft())

        async def competitor():
            async with rig.factory() as second:
                result = await rig.outbox.enqueue(second, rig.draft())
                with pytest.raises(DuplicateMessage):
                    await rig.outbox.enqueue(second, rig.draft(request_raw=b"different"))
                await rig.outbox.enqueue(second, rig.draft("another"))
                await second.commit()
                return result

        pending = asyncio.create_task(competitor())
        try:
            await wait_for_database_lock(rig.factory, pending)
            await (first.rollback() if rollback else first.commit())
            async with asyncio.timeout(5):
                result = await pending
            assert (result.message_id == original.message_id) is not rollback
        finally:
            await first.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert len(rows) == 2 and all(row.request_raw == b"\x00opaque" for row in rows)


async def test_cleanup_between_conflict_and_readback_creates_new_valid_row(pg_rig):
    rig = pg_rig
    initial, proceed, conflict, readback = (asyncio.Event() for _ in range(4))

    class RacingSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            result = await super().scalar(statement, *args, **kwargs)
            if not initial.is_set():
                assert result is None
                initial.set()
                await proceed.wait()
            elif isinstance(statement, Insert) and result is None:
                conflict.set()
                await readback.wait()
            return result

    async def insert():
        factory = async_sessionmaker(rig.engine, class_=RacingSession)
        async with factory() as session:
            result = await rig.outbox.enqueue(session, rig.draft())
            await session.commit()
            return result

    pending = asyncio.create_task(insert())
    try:
        async with asyncio.timeout(5):
            await initial.wait()
            async with rig.factory() as session:
                old = await rig.outbox.enqueue(session, rig.draft())
                await session.commit()
            proceed.set()
            await conflict.wait()
            async with rig.factory() as session:
                await session.execute(
                    delete(ReliableMessage).where(ReliableMessage.id == old.message_id)
                )
                await session.commit()
            readback.set()
            result = await pending
            assert result.message_id != old.message_id
    finally:
        proceed.set()
        readback.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_two_workers_claim_and_recovery_fence_have_one_winner(pg_rig, delivered):
    rig = pg_rig
    async with rig.factory() as session:
        for number in range(8):
            await rig.outbox.enqueue(session, rig.draft(str(number)))
        await session.commit()
    other = MessageStore(rig.factory, clock=rig.clock, uniform=lambda low, high: high)
    first, second = await asyncio.gather(
        rig.store.claim(4, rig.targets), other.claim(4, rig.targets)
    )
    assert len(first) == len(second) == 4
    assert {item.message_id for item in first}.isdisjoint(item.message_id for item in second)
    rig.clock.advance(15)
    old = first[0]
    completed, recovered = await asyncio.gather(
        rig.store.finish(old, delivered),
        other.recover_expired(8, rig.targets),
    )
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, old.message_id)
        assert row.attempts == 1 and row.status in ("DONE", "PENDING")
        assert (row.status == "DONE") == completed
    assert recovered in (7, 8)
    assert not await rig.store.finish(old, delivered)


async def test_expiry_and_claim_have_one_transition_winner(pg_rig):
    rig = pg_rig
    async with rig.factory() as session:
        await rig.outbox.enqueue(session, rig.draft(ttl=timedelta(microseconds=1)))
        await session.commit()
    rig.clock.advance(0.000001)
    expired, claimed = await asyncio.gather(
        rig.store.expire_pending(1), rig.store.claim(1, rig.targets)
    )
    assert expired == 1 and not claimed


@pytest.mark.parametrize("phase", ["after_claim", "after_send"])
async def test_process_crash_recovers_original_message(pg_rig, pg_url, tmp_path, phase):
    rig = pg_rig
    async with rig.factory() as session:
        original = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    evidence = tmp_path / "requests.jsonl"

    async def child(mode, now):
        env = {
            **os.environ,
            "TEST_DATABASE_URL": pg_url,
            "CRASH_TARGET": rig.target.name,
            "CRASH_NOW": now.isoformat(),
            "CRASH_PHASE": mode,
            "CRASH_EVIDENCE": str(evidence),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.reliable.crash_worker",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(10):
                stdout, stderr = await process.communicate()
            assert not stdout and not stderr, (stdout, stderr)
            return process.returncode
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    assert await child(phase, rig.clock()) == 73
    assert not evidence.with_suffix(".closed").exists()
    before = evidence.read_text().splitlines() if evidence.exists() else []
    assert len(before) == (phase == "after_send")
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, original.message_id)
        assert row.status == "SENDING" and row.attempts == 0
        assert row.response_raw is None and row.last_http_status is None
        assert row.lease_until.tzinfo is not None
    assert await child("recover", rig.clock() + timedelta(seconds=16)) == 0
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert len(records) == 1 + (phase == "after_send")
    assert all(record == records[0] for record in records)
    assert records[0]["body"] == b"\x00opaque".hex()
    assert records[0]["headers"] == [["X-Operation", "opaque"], ["X-Operation-Id", "id"]]
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, original.message_id)
        assert row.status == "DONE" and row.attempts == 2 and row.response_raw == b"accepted"
        assert row.operation_id == "id" and row.request_raw == b"\x00opaque"


async def test_empty_targets_continue_recovery_without_http(pg_rig, monkeypatch):
    from app.reliable.sender import Sender

    rig = pg_rig
    async with rig.factory() as session:
        for identity in ("pending", "sending", "expired"):
            await rig.outbox.enqueue(
                session,
                rig.draft(identity, ttl=timedelta(seconds=1) if identity == "expired" else None),
            )
        await session.commit()
    claims = await rig.store.claim(2, rig.targets)
    rig.clock.advance(1)
    sender = Sender(rig.store, {}, {}, {}, clock=rig.clock, uniform=lambda low, high: high)
    assert await sender.tick() == 0
    async with rig.factory() as session:
        for claim in claims:
            assert (await session.get(ReliableMessage, claim.message_id)).status == "SENDING"
        await rig.outbox.enqueue(session, rig.draft("later"))
        await session.commit()
    await sender.tick()
    await asyncio.gather(*sender.inflight)
    rig.clock.advance(14)
    await sender.tick()
    rig.clock.advance(1)
    await sender.tick()
    await sender.aclose()
    async with rig.factory() as session:
        rows = list(
            await session.scalars(
                select(ReliableMessage).where(ReliableMessage.target == rig.target.name)
            )
        )
        assert {row.status for row in rows} == {"EXPIRED", "PENDING"}
        assert {row.operation_id: row.attempts for row in rows} == {
            "pending": 1,
            "sending": 1,
            "expired": 0,
            "later": 0,
        }
        assert all(row.lease_id is None for row in rows)
        assert all(row.last_http_status is None for row in rows)


async def test_sending_holds_no_database_connection_and_save_failure_keeps_lease(pg_rig):
    rig = pg_rig
    async with rig.factory() as session:
        message = await rig.outbox.enqueue(session, rig.draft())
        await session.commit()
    (claim,) = await rig.store.claim(1, rig.targets)
    checked = asyncio.Event()

    class Client:
        async def send(self, request):
            assert rig.engine.pool.checkedout() == 0
            async with rig.factory() as session:
                assert await session.scalar(text("SELECT 1")) == 1
            checked.set()
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

    async def fail_save(*args):
        raise RuntimeError("storage unavailable")

    rig.store.finish = fail_save
    with pytest.raises(RuntimeError, match="storage"):
        await send_one(
            claim,
            store=rig.store,
            targets=rig.targets,
            adapters=rig.adapters,
            clients={rig.target.name: Client()},
            clock=rig.clock,
            uniform=lambda low, high: high,
        )
    assert checked.is_set()
    async with rig.factory() as session:
        row = await session.get(ReliableMessage, message.message_id)
        assert row.status == "SENDING" and row.attempts == 0 and row.response_raw is None


@pytest.mark.parametrize("rollback", [False, True])
async def test_concurrent_enqueue_returns_persisted_winner_business_key(pg_rig, rollback):
    rig = pg_rig
    async with rig.factory() as first:
        original = await rig.outbox.enqueue(first, rig.draft(business_key="opaque:first"))

        async def compete():
            async with rig.factory.begin() as second:
                return await rig.outbox.enqueue(second, rig.draft(business_key="opaque:second"))

        pending = asyncio.create_task(compete())
        try:
            await wait_for_database_lock(rig.factory, pending)
            await (first.rollback() if rollback else first.commit())
            async with asyncio.timeout(5):
                result = await pending
            assert result.business_key == ("opaque:second" if rollback else "opaque:first")
            assert (result.message_id == original.message_id) is not rollback
        finally:
            await first.rollback()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
