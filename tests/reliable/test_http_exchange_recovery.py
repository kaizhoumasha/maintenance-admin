from dataclasses import replace

import pytest
from sqlalchemy import text

from app.http_client import HttpRequest
from app.integrations.http_exchanges import reconcile_outbound
from app.models import ReliableMessage
from app.reliable.config import TargetConfig


@pytest.mark.parametrize("late", [False, True])
async def test_settlement_can_restore_original_exchange_without_sender(storage, delivered, late):
    rig = storage
    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    rig.outbox.targets[target.name] = target
    rig.outbox.adapters[target.protocol] = rig.adapter
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, {target.name: target})
    request = HttpRequest("POST", "/decisions", body=claim.message.request_raw)
    assert await rig.store.before_send(replace(claim, request=request), target)

    async with rig.factory() as session:
        assert await rig.outbox.settle(session, target.name, "opaque", "id")
        await session.rollback()
    await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        assert (
            await session.scalar(
                text("SELECT attempt_state FROM wms_http_exchange WHERE message_id=:id"),
                {"id": claim.message_id},
            )
            == "OPEN"
        )

    async with rig.factory.begin() as session:
        assert await rig.outbox.settle(session, target.name, "opaque", "id")
    if late:
        rig.clock.advance(1)
        assert not await rig.store.finish(claim, delivered)

    # Emulate loss of the journal and all in-memory work at the process boundary.
    async with rig.factory.begin() as session:
        await session.execute(
            text("DELETE FROM wms_http_exchange WHERE message_id=:id"), {"id": claim.message_id}
        )
    await reconcile_outbound(rig.factory, now=rig.clock())
    await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        result = (
            await session.execute(
                text(
                    "SELECT close_reason, attempt_state, request_wire, response_wire, "
                    "late_response_at, initiator FROM wms_http_exchange WHERE message_id=:id"
                ),
                {"id": claim.message_id},
            )
        ).one()
        assert result[:3] == ("SETTLED_BY_CALLBACK", "DELIVERY_UNKNOWN", request.body)
        assert result.initiator == "WES"
        assert result.response_wire == (b"ok" if late else None)
        assert (result.late_response_at is not None) == late


async def test_outbound_projection_failure_leaves_committed_settlement_recoverable(
    storage, monkeypatch
):
    from sqlalchemy import event

    rig = storage
    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    rig.outbox.targets[target.name] = target
    rig.outbox.adapters[target.protocol] = rig.adapter
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, {target.name: target})
    request = HttpRequest("POST", "/decisions", body=claim.message.request_raw)
    assert await rig.store.before_send(replace(claim, request=request), target)
    async with rig.factory.begin() as session:
        assert await rig.outbox.settle(session, target.name, "opaque", "id")

    def fail_projection(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO" in statement and "wms_http_exchange" in statement:
            raise ConnectionError("simulated diagnostic write failure")

    event.listen(rig.engine.sync_engine, "before_cursor_execute", fail_projection)
    try:
        with pytest.raises(ConnectionError):
            await reconcile_outbound(rig.factory, now=rig.clock())
    finally:
        event.remove(rig.engine.sync_engine, "before_cursor_execute", fail_projection)
    await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        assert (
            await session.scalar(
                text("SELECT close_reason FROM wms_http_exchange WHERE message_id=:id"),
                {"id": claim.message_id},
            )
            == "SETTLED_BY_CALLBACK"
        )


async def test_expired_lease_response_updates_only_original_exchange(storage, delivered):
    import asyncio

    rig = storage
    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    targets = {target.name: target}
    rig.outbox.targets.update(targets)
    rig.outbox.adapters[target.protocol] = rig.adapter
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft())
    (first,) = await rig.store.claim(1, targets)
    request = HttpRequest("POST", "/decisions", body=first.message.request_raw)
    assert await rig.store.before_send(replace(first, request=request), target)

    rig.clock.advance(15)
    assert await rig.store.recover_expired(1, targets) == 1
    rig.clock.advance(1)
    (second,) = await rig.store.claim(1, targets)
    assert await rig.store.before_send(replace(second, request=request), target)
    await reconcile_outbound(rig.factory, now=rig.clock())

    completed, _ = await asyncio.gather(
        rig.store.finish(first, delivered),
        reconcile_outbound(rig.factory, now=rig.clock()),
    )
    assert not completed
    assert not await rig.store.finish(first, delivered)
    await reconcile_outbound(rig.factory, now=rig.clock())

    async with rig.factory() as session:
        message = await session.get(ReliableMessage, first.message_id)
        assert message.status == "SENDING" and message.lease_id == second.lease_id
        rows = await session.execute(
            text(
                "SELECT lease_id, close_reason, attempt_state, response_wire, late_response_at "
                "FROM wms_http_exchange WHERE message_id=:id"
            ),
            {"id": first.message_id},
        )
        records = {row.lease_id: row for row in rows}
        assert set(records) == {first.lease_id, second.lease_id}
        original, current = records[first.lease_id], records[second.lease_id]
        assert original[1:4] == ("LEASE_EXPIRED", "DELIVERY_UNKNOWN", b"ok")
        assert original.late_response_at is not None
        assert current[1:] == (None, "OPEN", None, None)

    assert await rig.store.finish(second, delivered)
    assert not await rig.store.finish(second, delivered)
    await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        assert (
            await session.scalar(
                text("SELECT late_response_at FROM wms_http_exchange WHERE lease_id=:lease"),
                {"lease": second.lease_id},
            )
            is None
        )


async def test_retention_does_not_recreate_an_attempt_expired_during_projection(
    pg_rig, wait_for_lock
):
    import asyncio
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.dml import Insert

    from app.integrations.http_exchanges import cleanup_exchanges

    rig = pg_rig
    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    rig.outbox.targets[target.name] = target
    rig.outbox.adapters[target.protocol] = rig.adapter
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, {target.name: target})
    request = HttpRequest("POST", "/decisions", body=claim.message.request_raw)
    assert await rig.store.before_send(replace(claim, request=request), target)
    async with rig.factory.begin() as session:
        assert await rig.outbox.settle(session, target.name, "opaque", "id")

    loaded = asyncio.Event()
    finish = asyncio.Event()

    class PausedProjection(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Insert) and statement.table.name == "wms_http_exchange":
                loaded.set()
                await finish.wait()
            return await super().execute(statement, *args, **kwargs)

    projection_factory = async_sessionmaker(rig.engine, class_=PausedProjection)
    projection = asyncio.create_task(reconcile_outbound(projection_factory, now=rig.clock()))
    cleanup = None
    try:
        await asyncio.wait_for(loaded.wait(), 5)
        cleanup = asyncio.create_task(
            cleanup_exchanges(rig.factory, 90, now=rig.clock() + timedelta(days=91))
        )
        await wait_for_lock(cleanup)
    finally:
        finish.set()
        await asyncio.gather(*([projection, cleanup] if cleanup else [projection]))
    async with rig.factory() as session:
        for table in ("reliable_delivery_attempt", "wms_http_exchange"):
            assert (
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE message_id=:id"),
                    {"id": claim.message_id},
                )
                == 0
            )
