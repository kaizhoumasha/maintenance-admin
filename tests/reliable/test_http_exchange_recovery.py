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
                    "late_response_at, initiator, peer_endpoint "
                    "FROM wms_http_exchange WHERE message_id=:id"
                ),
                {"id": claim.message_id},
            )
        ).one()
        assert result[:3] == ("SETTLED_BY_CALLBACK", "DELIVERY_UNKNOWN", request.body)
        assert result.initiator == "WES"
        assert result.peer_endpoint == target.base_url
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


@pytest.mark.parametrize("state", ["in-flight", "owner-held", "completed", "projection-pending"])
async def test_runtime_retention_of_original_wms_reply(storage, delivered, monkeypatch, state):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from fastapi import BackgroundTasks, Request

    from app.businesses.coarse_sorting.request.inbound_coarse_sorting_validate_bind_v1 import (
        ValidateBind,
    )
    from app.controllers.integration_history import get_http_body
    from app.core.config import get_settings
    from app.core.errors import AppError, ErrorCode
    from app.core.identifiers import uuid7
    from app.integrations.history import SourceRef, link_source
    from app.integrations.http_exchanges import cleanup_exchanges
    from app.integrations.wms_request import ADAPTERS, WmsRequests
    from app.models import ReliableDeliveryAttempt

    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", True)
    rig = storage
    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    targets = {target.name: target}
    rig.outbox.targets.update(targets)
    adapter = rig.outbox.adapters[target.protocol] = ADAPTERS[target.protocol]
    operation_id = str(uuid7())
    wms = WmsRequests(
        rig.outbox, owner_domain="coarse_sorting", contract_consumers=lambda _: {"coarse_sorting"}
    )
    async with rig.factory.begin() as session:
        reference = await wms.send(
            session,
            ValidateBind(workline_code="A", barcode="B", reel_diameter=7, reel_thickness=15),
            operation_id=operation_id,
        )
        if state == "owner-held":
            await link_source(
                session,
                source=SourceRef("WMS_MESSAGE", str(reference.message_id)),
                owner_domain="coarse_sorting",
                reference_kind="BUSINESS_TASK",
                namespace="coarse_sorting",
                value=str(uuid7()),
                relation_role="OWNER",
                at=rig.clock(),
            )

    (first,) = await rig.store.claim(1, targets)
    request = adapter.build_request(first.message, target)
    assert await rig.store.before_send(replace(first, request=request), target)
    rig.clock.advance(15)
    assert await rig.store.recover_expired(1, targets) == 1
    rig.clock.advance(1)
    (second,) = await rig.store.claim(1, targets)
    assert await rig.store.before_send(replace(second, request=request), target)

    def reply(claim, binding):
        raw = json.dumps(
            {
                "operation_id": operation_id,
                "code": "DECIDED",
                "timestamp": 1,
                "data": {"result": "OK", "binding_id": binding},
            }
        ).encode()
        result = replace(delivered.result, body=raw)
        return replace(
            delivered, result=result, verdict=adapter.classify_response(claim.message, result)
        )

    late_reply = reply(first, "BIND-ORIGINAL-OBSERVED")
    assert not await rig.store.finish(first, late_reply)
    settled = state in {"owner-held", "completed"}
    terminal_reply = reply(second, "BIND-CURRENT-APPLIED")
    if settled:
        assert await rig.store.finish(second, terminal_reply)
    if state != "projection-pending":
        await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        original = await session.get(ReliableDeliveryAttempt, (first.message_id, first.lease_id))
        assert original is not None and original.response_wire == late_reply.result.body
        original_id = original.id

    after_retention = rig.clock() + timedelta(days=91)
    await cleanup_exchanges(rig.factory, 90, now=after_retention)
    if state == "projection-pending":
        await reconcile_outbound(rig.factory, now=after_retention)
    async with rig.factory() as session:
        message = await session.get(ReliableMessage, reference.message_id)
        assert (message.operation_id, message.status, message.lease_id) == (
            operation_id,
            "DONE" if settled else "SENDING",
            None if settled else second.lease_id,
        )
        assert message.terminal_response_wire == (terminal_reply.result.body if settled else None)
        original = await session.get(ReliableDeliveryAttempt, (first.message_id, first.lease_id))

    async def read_original():
        app = SimpleNamespace(
            state=SimpleNamespace(
                integration_history_session_factory=rig.factory, audit_session_factory=rig.factory
            )
        )
        return await get_http_body(
            Request({"type": "http", "app": app, "headers": []}),
            "WMS",
            original_id,
            "response",
            BackgroundTasks(),
            SimpleNamespace(id=1),
        )

    if state == "completed":
        assert original is None
        with pytest.raises(AppError) as missing:
            await read_original()
        assert missing.value.code == ErrorCode.NOT_FOUND
    else:
        assert (await read_original()).body == late_reply.result.body
        assert original is not None and original.response_wire == late_reply.result.body


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


async def test_cleanup_removes_expired_unprojected_attempt(rig):
    from datetime import timedelta

    from app.integrations.http_exchanges import cleanup_exchanges

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

    await cleanup_exchanges(rig.factory, 90, now=rig.clock() + timedelta(days=91))
    async with rig.factory() as session:
        for table in ("reliable_delivery_attempt", "http_projection_pending"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_sqlite_projection_keeps_settlement_committed_after_read(rig):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.dml import Insert

    target = TargetConfig(rig.target.name, rig.target.base_url, "wms-decisions")
    rig.outbox.targets[target.name] = target
    rig.outbox.adapters[target.protocol] = rig.adapter
    async with rig.factory.begin() as session:
        await rig.outbox.enqueue(session, rig.draft())
    (claim,) = await rig.store.claim(1, {target.name: target})
    request = HttpRequest("POST", "/decisions", body=claim.message.request_raw)
    assert await rig.store.before_send(replace(claim, request=request), target)

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
    try:
        await asyncio.wait_for(loaded.wait(), 5)
        async with rig.factory.begin() as session:
            assert await rig.outbox.settle(session, target.name, "opaque", "id")
    finally:
        finish.set()
        await projection

    await reconcile_outbound(rig.factory, now=rig.clock())
    async with rig.factory() as session:
        assert (
            await session.scalar(
                text("SELECT close_reason FROM wms_http_exchange WHERE message_id = :id"),
                {"id": claim.message_id},
            )
            == "SETTLED_BY_CALLBACK"
        )
