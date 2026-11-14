import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from app.integrations.ecs_command.service import CommandOwner
from app.models import ReliableDeliveryAttempt, ReliableMessage
from tests.helpers import EcsMove, loopback_app, simulator_sender


@pytest.mark.parametrize("owner", ["coarse_sorting", "xray"])
async def test_coarse_command_waits_for_fresh_idle_and_sends_original(ecs, owner):
    peer = FastAPI()
    states = iter(("RUNNING", None, "IDLE"))
    requests = []

    @peer.get("/api/v1/device/status")
    async def status(device_code: str):
        requests.append(("status", device_code))
        state = next(states)
        return {
            "devices": []
            if state is None
            else [
                {
                    "device": dict.fromkeys(
                        (
                            "device_name",
                            "device_type",
                            "role",
                            "supported_commands",
                            "supported_events",
                        )
                    )
                    | {"device_code": device_code},
                    "state": {
                        "device_code": device_code,
                        "mode": "AUTO",
                        "status": state,
                        "is_online": True,
                        "current_command_code": None,
                        "scenario": None,
                        "updated_at": 1,
                    },
                }
            ]
        }

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
                call_key="status-gate",
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


@pytest.mark.parametrize("interruption", ["release", "expired", "deadline"])
async def test_unstarted_coarse_command_recovers_without_false_delivery(ecs, interruption):
    ecs.routes.origin = "http://127.0.0.1:9999"
    now = datetime.now(UTC)
    ecs.service.clock = ecs.outbox.clock = lambda: now
    async with ecs.factory.begin() as session:
        original = await ecs.service.create(
            session,
            owner=CommandOwner("coarse_sorting"),
            call_key="recover-status-check",
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
            assert not await sender.store.claim(1, ecs.targets)
            async with ecs.factory() as session:
                message = await session.get(ReliableMessage, original.message_id)
                assert message.status == "EXPIRED" and not message.delivery_uncertain
        else:
            (retry,) = await sender.store.claim(1, ecs.targets)
            assert retry.message_id == original.message_id
            assert retry.message.request_raw == claim.message.request_raw
            assert retry.attempt_no == 1 and not retry.delivery_uncertain
