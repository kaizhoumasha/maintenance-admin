import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from app.core.identifiers import uuid7
from app.integrations.ecs_command.service import CommandOwner
from app.models import ReliableDeliveryAttempt, ReliableMessage
from tests.helpers import EcsMove, ecs_status_document, loopback_app, simulator_sender


@pytest.mark.parametrize("owner", ["coarse_sorting", "xray"])
async def test_coarse_command_waits_for_fresh_idle_and_sends_original(ecs, owner):
    peer = FastAPI()
    states = iter(("RUNNING", None, "IDLE"))
    requests = []

    @peer.get("/api/v1/device/status")
    async def status(device_code: str):
        requests.append(("status", device_code))
        state = next(states)
        return {"devices": []} if state is None else ecs_status_document(device_code, status=state)

    @peer.post("/api/v1/device/command")
    async def command(request: Request):
        requests.append(("command", await request.body()))
        return {"code": 200, "message": "ACK"}

    async with loopback_app(peer) as origin:
        ecs.routes.origin = origin
        async with ecs.factory.begin() as session:
            original = await ecs.service.create(
                session,
                owner=CommandOwner(owner),
                call_key=str(uuid7()),
                device_code="D1",
                timeout_ms=30000,
                command=EcsMove(qty=1),
            )

        now = datetime.now(UTC)
        async with simulator_sender(ecs, (origin,)) as sender:
            sender.clock = sender.store.clock = lambda: now
            for checks in range(1, 3) if owner == "coarse_sorting" else ():
                assert await sender.tick(maintain=False) == 1
                await asyncio.gather(*sender.inflight)
                assert requests == [("status", "D1")] * checks
                async with ecs.factory() as session:
                    item = await session.get(ReliableMessage, original.message_id)
                    assert item.status == "PENDING"
                    assert item.attempts == item.not_received_count == 0
                    assert item.dispatch_started_at is None
                    assert not await session.scalar(
                        select(ReliableDeliveryAttempt.id).where(
                            ReliableDeliveryAttempt.message_id == item.id
                        )
                    )
                assert await sender.tick(maintain=False) == 0
                now += timedelta(seconds=10)

            assert await sender.tick(maintain=False) == 1
            await asyncio.gather(*sender.inflight)

        assert [kind for kind, _ in requests] == (
            ["status", "status", "status", "command"] if owner == "coarse_sorting" else ["command"]
        )
        assert json.loads(requests[-1][1])["command_code"] == original.command_code
        async with ecs.factory() as session:
            item = await session.get(ReliableMessage, original.message_id)
            assert item.status == "DONE"
            assert item.attempts == 1


async def test_coarse_status_gate_renews_expiry_before_dispatch(ecs):
    peer = FastAPI()

    @peer.get("/api/v1/device/status")
    async def status(device_code: str):
        return ecs_status_document(device_code, status="RUNNING")

    async with loopback_app(peer) as origin:
        ecs.routes.origin = origin
        now = datetime.now(UTC)
        ecs.service.clock = ecs.outbox.clock = lambda: now
        async with ecs.factory.begin() as session:
            original = await ecs.service.create(
                session,
                owner=CommandOwner("coarse_sorting"),
                call_key=str(uuid7()),
                device_code="D1",
                timeout_ms=30000,
                command=EcsMove(qty=1),
            )

        async with simulator_sender(ecs, (origin,)) as sender:
            sender.clock = sender.store.clock = lambda: now
            assert await sender.tick(maintain=False) == 1
            await asyncio.gather(*sender.inflight)
            async with ecs.factory() as session:
                first = await session.get(ReliableMessage, original.message_id)
                first_expiry = first.expires_at.replace(tzinfo=UTC)

            now += timedelta(seconds=301)
            assert await sender.tick() == 1
            await asyncio.gather(*sender.inflight)
            async with ecs.factory() as session:
                renewed = await session.get(ReliableMessage, original.message_id)
                assert renewed.status == "PENDING"
                assert renewed.expires_at.replace(tzinfo=UTC) > first_expiry
                assert renewed.dispatch_started_at is None


@pytest.mark.parametrize("owner", ["coarse_sorting", "xray"])
async def test_ecs_expired_delivery_requeues_original(ecs, owner):
    from app.http_client import DeliveryState, HttpResult
    from app.integrations import history as history_records
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.reliable.delivery import send_one
    from app.reliable.store import MessageStore
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = ecs.outbox.clock = clock
    async with ecs.factory.begin() as session:
        original = await ecs.service.create(
            session,
            owner=CommandOwner(owner),
            call_key=str(uuid7()),
            device_code="D1",
            timeout_ms=30000,
            command=EcsMove(qty=1),
        )
    store = MessageStore(
        ecs.factory,
        lifecycles={"ecs-command": EcsLifecycle(ecs.service)},
        records=history_records,
        clock=clock,
    )
    (claim,) = await store.claim(1, ecs.targets)

    class Client:
        async def send(self, request):
            clock.advance(seconds=301)
            return HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE,
                None,
                503,
                (("content-type", "application/json"),),
                b'{"code":503,"message":"TEMPORARILY_UNAVAILABLE"}',
            )

        async def aclose(self):
            pass

    provider = DynamicClients(factory=lambda _: Client())
    await send_one(
        claim,
        store=store,
        targets=ecs.targets,
        adapters=ecs.adapters,
        clients={},
        provider=provider,
        clock=clock,
        uniform=lambda low, high: high,
    )
    async with ecs.factory() as session:
        message = await session.get(ReliableMessage, original.message_id)
        assert message.status == "PENDING"
        assert message.operation_id == original.command_code
        assert message.expires_at.replace(tzinfo=UTC) > clock()
        view = await ecs.service.get_command(
            session, owner=CommandOwner(owner), command_code=original.command_code
        )
        assert view.status == "QUEUED"
    await provider.aclose()


@pytest.mark.parametrize("window", [60, 600])
async def test_ecs_expired_delivery_keeps_frozen_retry_window(ecs, window):
    from app.http_client import DeliveryState, HttpResult
    from app.integrations import history as history_records
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.runtime_authority import enabled
    from app.models import EcsCommand, IntegrationExchange
    from app.reliable.delivery import send_one
    from app.reliable.store import MessageStore
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = ecs.outbox.clock = clock
    async with ecs.factory.begin() as session:
        original = await ecs.service.create(
            session,
            owner=CommandOwner("xray"),
            call_key=str(uuid7()),
            device_code="D1",
            timeout_ms=30000,
            command=EcsMove(qty=1),
        )
        row = await session.get(ReliableMessage, original.message_id)
        model = EcsCommand if enabled() else IntegrationExchange
        command_row = await session.scalar(
            select(model).where(model.command_code == original.command_code)
        )
        frozen = row.created_at + timedelta(seconds=window)
        command_row.route = {**command_row.route, "retry_window": window}
        command_row.expires_at = frozen
        row.expires_at = frozen

    store = MessageStore(
        ecs.factory,
        lifecycles={"ecs-command": EcsLifecycle(ecs.service)},
        records=history_records,
        clock=clock,
    )
    (claim,) = await store.claim(1, ecs.targets)

    class Client:
        async def send(self, request):
            clock.advance(seconds=window + 1)
            return HttpResult(
                DeliveryState.HTTP_RESPONSE_COMPLETE,
                None,
                503,
                (("content-type", "application/json"),),
                b'{"code":503,"message":"TEMPORARILY_UNAVAILABLE"}',
            )

        async def aclose(self):
            pass

    provider = DynamicClients(factory=lambda _: Client())
    await send_one(
        claim,
        store=store,
        targets=ecs.targets,
        adapters=ecs.adapters,
        clients={},
        provider=provider,
        clock=clock,
        uniform=lambda low, high: high,
    )
    async with ecs.factory() as session:
        message = await session.get(ReliableMessage, original.message_id)
        assert message.expires_at.replace(tzinfo=UTC) == clock() + timedelta(seconds=window)
    await provider.aclose()


@pytest.mark.parametrize("interruption", ["release", "expired", "deadline"])
async def test_unstarted_coarse_command_recovers_without_false_delivery(ecs, interruption):
    ecs.routes.origin = "http://127.0.0.1:9999"
    now = datetime.now(UTC)
    ecs.service.clock = ecs.outbox.clock = lambda: now
    async with ecs.factory.begin() as session:
        original = await ecs.service.create(
            session,
            owner=CommandOwner("coarse_sorting"),
            call_key=str(uuid7()),
            device_code="D1",
            timeout_ms=30000,
            command=EcsMove(qty=1),
        )

    async with simulator_sender(ecs, (ecs.routes.origin,)) as sender:
        sender.store.clock = lambda: now
        (claim,) = await sender.store.claim(1, ecs.targets)
        if interruption == "release":
            assert await sender.store.release(claim, ecs.targets)
        else:
            now += timedelta(seconds=301 if interruption == "deadline" else 30)
            assert await sender.store.maintain_snapshots(10, ecs.targets, time.monotonic() + 3)
        if interruption == "deadline":
            async with ecs.factory() as session:
                message = await session.get(ReliableMessage, original.message_id)
                assert message.status == "PENDING"
                assert message.expires_at.replace(tzinfo=UTC) > now
                assert not message.delivery_uncertain
            (retry,) = await sender.store.claim(1, ecs.targets)
            assert retry.message_id == original.message_id
        else:
            (retry,) = await sender.store.claim(1, ecs.targets)
            assert retry.message_id == original.message_id
            assert retry.message.request_raw == claim.message.request_raw
            assert retry.attempt_no == 1 and not retry.delivery_uncertain
