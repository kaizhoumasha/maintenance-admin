import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.http_client import DeliveryState, HttpClientConfig, HttpResult
from app.models import ReliableMessage
from app.reliable import MessageDraft
from app.reliable.types import DeliverySnapshot, DuplicateMessage


async def test_snapshot_is_part_of_identity_and_hold_is_not_resumable(ecs):
    async with ecs.factory.begin() as session:
        command = await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
    async with ecs.factory.begin() as session:
        row = await session.scalar(select(ReliableMessage))
        draft = MessageDraft(
            row.target,
            row.operation,
            row.operation_id,
            row.request_raw,
            delivery_snapshot=DeliverySnapshot(**row.delivery_snapshot),
        )
        with pytest.raises(DuplicateMessage):
            await ecs.outbox.enqueue(
                session,
                replace(
                    draft,
                    delivery_snapshot=replace(draft.delivery_snapshot, origin="http://other.test"),
                ),
            )
        assert await ecs.outbox.hold(
            session, "ecs", "ecs.command", command.command_code, reason="DELIVERY_UNKNOWN"
        )
        assert not await ecs.outbox.resume(session, "ecs", "ecs.command", command.command_code)
        await session.refresh(row)
        assert row.status == "HELD" and row.next_attempt_at is None


@pytest.fixture
async def delivery(ecs):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.delivery import send_one
    from app.reliable.store import MessageStore
    from app.reliable.testing import ScriptedHttpClient

    async with ecs.factory.begin() as session:
        await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key="call",
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    (claim,) = await store.claim(1, ecs.targets)
    client = ScriptedHttpClient(
        HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b'{"code":200,"message":"ACK"}'
        )
    )
    provider = DynamicClients(factory=lambda config: client, capacity=1)
    args = dict(
        claim=claim,
        store=store,
        targets=dict(ecs.targets),
        adapters=dict(ecs.adapters),
        clients={},
        provider=provider,
        clock=ecs.service.clock,
        uniform=lambda low, high: high,
    )
    try:
        yield SimpleNamespace(
            client=client, provider=provider, store=store, claim=claim, args=args, send=send_one
        )
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "stage,phase,reason,gated",
    [
        ("prepare", "prepare", "CONFIG_DRIFT", False),
        ("build_request", "build_request", "PRE_GATE_VALIDATION", False),
        ("acquire", "acquire_client", "PRE_GATE_VALIDATION", False),
        ("before_send", "before_send", "PRE_GATE_VALIDATION", False),
        ("send", "send", "DELIVERY_UNKNOWN", True),
        ("invalid_result", "send", "DELIVERY_UNKNOWN", True),
        ("classify_response", "classify_response", "DELIVERY_UNKNOWN", True),
        ("release", "acquire_client", "DELIVERY_UNKNOWN", True),
        ("release_without_gate", "acquire_client", "PRE_GATE_VALIDATION", False),
        ("apply_outcome", "apply_outcome", "DELIVERY_UNKNOWN", True),
    ],
)
async def test_snapshot_failure_reports_phase_and_preserves_hold(
    ecs, delivery, monkeypatch, caplog, stage, phase, reason, gated
):
    error = RuntimeError("private_sql private_token http://user:private_password@host private_body")

    def fail(*args):
        raise error

    async def async_fail(*args):
        raise error

    adapter = next(iter(delivery.args["adapters"].values()))
    original_borrow = delivery.provider.borrow

    @asynccontextmanager
    async def borrow(config):
        if stage == "acquire":
            raise error
        async with original_borrow(config) as client:
            yield client
        if stage in ("release", "release_without_gate"):
            raise error

    if stage == "prepare":
        delivery.args["targets"].clear()
    elif stage == "build_request":
        monkeypatch.setattr(adapter, "build_request", fail)
    elif stage in ("acquire", "release", "release_without_gate"):
        monkeypatch.setattr(delivery.provider, "borrow", borrow)
        if stage == "release_without_gate":

            async def declined(*args):
                return None

            monkeypatch.setattr(delivery.store, "before_send", declined)
    elif stage == "before_send":
        monkeypatch.setattr(delivery.store, "before_send", async_fail)
    elif stage == "send":
        monkeypatch.setattr(delivery.client, "send", async_fail)
    elif stage == "invalid_result":

        async def invalid(*args):
            return None

        monkeypatch.setattr(delivery.client, "send", invalid)
    elif stage == "classify_response":
        monkeypatch.setattr(adapter, "classify_response", fail)
    elif stage == "apply_outcome":

        async def not_sent(*args):
            return HttpResult(DeliveryState.NOT_SENT, None, None, (), None)

        monkeypatch.setattr(delivery.client, "send", not_sent)
        delivery.args["uniform"] = fail
    with caplog.at_level(logging.ERROR):
        await delivery.send(**delivery.args)
    async with ecs.factory() as session:
        row = await session.get(ReliableMessage, delivery.claim.message_id)
        assert row.status == "HELD" and row.pause_reason == reason
        assert row.delivery_uncertain is gated
        assert row.request_raw == delivery.claim.message.request_raw
        assert row.response_raw is None
    records = [r for r in caplog.records if r.getMessage().startswith("reliable delivery failed ")]
    assert len(records) == 1 and records[0].phase == phase
    assert records[0].component == "reliable" and records[0].message_id == delivery.claim.message_id
    assert "origin=app/" in records[0].getMessage() and "private_" not in caplog.text
    monkeypatch.setattr(delivery.provider, "borrow", original_borrow)
    async with delivery.provider.borrow(HttpClientConfig("http://other.test", "probe")):
        pass  # 另一 origin 可借用容量为 1 的池,证明原借用已释放。


@pytest.mark.parametrize("failure", ["cancel", "finish"])
async def test_snapshot_cancellation_and_finish_failure_propagate(
    ecs, delivery, monkeypatch, caplog, failure
):
    async def fail(*args):
        raise asyncio.CancelledError() if failure == "cancel" else RuntimeError("private_storage")

    if failure == "cancel":
        monkeypatch.setattr(delivery.client, "send", fail)
    else:
        monkeypatch.setattr(delivery.store, "finish", fail)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await delivery.send(**delivery.args)
    async with ecs.factory() as session:
        row = await session.get(ReliableMessage, delivery.claim.message_id)
        assert row.status == "SENDING" and row.attempts == 0
        assert row.response_raw is None and row.lease_id == delivery.claim.lease_id
    assert not any(r.getMessage().startswith("reliable delivery failed ") for r in caplog.records)
    async with delivery.provider.borrow(HttpClientConfig("http://other.test", "probe")):
        pass


async def test_pool_unavailable_keeps_original_retry_without_failure_log(
    ecs, delivery, monkeypatch, caplog
):
    from app.reliable.client_provider import PoolUnavailable

    @asynccontextmanager
    async def unavailable(config):
        raise PoolUnavailable("private_capacity")
        yield

    monkeypatch.setattr(delivery.provider, "borrow", unavailable)
    await delivery.send(**delivery.args)
    async with ecs.factory() as session:
        row = await session.get(ReliableMessage, delivery.claim.message_id)
        assert row.status == "PENDING" and not row.delivery_uncertain
        assert row.next_attempt_at is not None and row.attempts == 1
    assert not any(r.getMessage().startswith("reliable delivery failed ") for r in caplog.records)
