"""Process restart preserves frozen obligations and the original retry schedule."""

import asyncio
import json
import time
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.http_client import DeliveryState, HttpResult
from app.integrations.ecs_command.clients import DynamicClients
from app.integrations.ecs_command.dev import DevEcsRoutes
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL
from app.models import EcsCommand, EcsSendEvidence, ReliableMessage
from app.reliable import lifecycle
from app.reliable.sender import Sender
from app.reliable.store import MessageStore
from app.reliable.testing import FakeClock, ScriptedHttpClient
from tests.helpers import EcsMove, ecs_protocol_annexes


def reply(code=200, message="ACK", retry_after=None):
    headers = (("content-type", "application/json"),)
    if retry_after is not None:
        headers += (("Retry-After", retry_after),)
    return HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        code,
        headers,
        json.dumps({"code": code, "message": message}).encode(),
    )


@pytest.fixture
async def restart(ecs, request):
    clock = FakeClock()
    ecs.service.clock = ecs.outbox.clock = clock
    historical_window = getattr(request, "param", None)
    if historical_window is None:
        ecs.service.annexes = ecs_protocol_annexes()
        ecs.service.routes = DevEcsRoutes().for_origin("http://127.0.0.1:8000")
    else:
        ecs.routes.origin = "http://127.0.0.1:8000"
    async with ecs.factory.begin() as session:
        if historical_window is None:
            from app.integrations.ecs_command.dev import create_debug_command

            async def authorize():
                pass

            await create_debug_command(
                session,
                service=ecs.service,
                owner=ecs.owner,
                client_request_id="restart",
                ecs_url="http://127.0.0.1:8000",
                device_code="D1",
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
                authorize_send=authorize,
            )
        else:
            await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key="restart",
                device_code="D1",
                timeout_ms=30000,
                command=EcsMove(qty=1),
            )
    if historical_window is not None:
        # A pre-upgrade formal snapshot fixture; no old dev policy is sent.
        async with ecs.factory.begin() as session:
            row = await session.scalar(select(EcsCommand))
            row.route = {**row.route, "retry_window": historical_window}
            row.expires_at = row.created_at + timedelta(seconds=historical_window)
            message = await session.scalar(select(ReliableMessage))
            message.expires_at = row.expires_at
    senders = []

    def start(client):
        sender = Sender(
            MessageStore(
                ecs.factory, clock=clock, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)}
            ),
            ecs.targets,
            ecs.adapters,
            {},
            provider=DynamicClients(factory=lambda _: client),
            clock=clock,
            monotonic=clock.monotonic,
            uniform=lambda a, b: b,
        )
        senders.append(sender)
        return sender

    try:
        yield clock, start
    finally:
        for sender in senders:
            await sender.aclose()


async def dispatch_one(sender):
    assert await sender.tick() == 1
    async with asyncio.timeout(5):
        await asyncio.gather(*list(sender.inflight))


async def wait_for_expiry(ecs, sender):
    # A bounded maintenance round can roll back before reaching this command.
    async with asyncio.timeout(5):
        while True:
            assert await sender.tick() == 0
            status = (await state(ecs))["status"]
            if status != "PENDING":
                break
            await asyncio.sleep(0)
    assert status == "EXPIRED"


async def state(ecs):
    async with ecs.factory() as session:
        command = await session.scalar(select(EcsCommand))
        message = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == "ecs")
        )
        evidence = (
            await session.execute(
                select(
                    EcsSendEvidence.lease_id, EcsSendEvidence.outcome, EcsSendEvidence.response_raw
                )
                .where(EcsSendEvidence.command_code == command.command_code)
                .order_by(EcsSendEvidence.id)
            )
        ).all()
        return {
            "frozen": {
                name: getattr(command, name)
                for name in (
                    "command_code",
                    "call_key",
                    "route",
                    "request_raw",
                    "delivery_snapshot",
                    "expires_at",
                )
            },
            "status": message.status,
            "command_status": command.status,
            "delivery_status": command.delivery_status,
            "attempts": message.attempts,
            "not_received": message.not_received_count,
            "next": message.next_attempt_at.replace(tzinfo=UTC)
            if message.next_attempt_at
            else None,
            "evidence": evidence,
        }


@pytest.mark.parametrize("stage", ["unsent", "not_sent", "503", "429"])
@pytest.mark.parametrize("expired", [False, True], ids=["within-deadline", "past-deadline"])
async def test_restart_preserves_safe_retry_schedule_and_frozen_command(
    ecs, restart, stage, expired
):
    await assert_safe_restart(ecs, restart, stage, expired)


@pytest.mark.parametrize("restart", [60, 600], indirect=True)
@pytest.mark.parametrize("stage", ["unsent", "not_sent", "503", "429"])
@pytest.mark.parametrize("expired", [False, True], ids=["within-deadline", "past-deadline"])
async def test_formal_pending_commands_keep_frozen_deadline_after_restart(
    ecs, restart, stage, expired
):
    await assert_safe_restart(ecs, restart, stage, expired)


async def assert_safe_restart(ecs, restart, stage, expired):
    clock, start = restart
    has_attempt = stage != "unsent"
    retry_after = 30 if (await state(ecs))["frozen"]["route"]["retry_window"] == 60 else 120
    initial = {
        "unsent": reply(),
        "not_sent": HttpResult(DeliveryState.NOT_SENT, None, None, (), None),
        "503": reply(503, "TEMPORARILY_UNAVAILABLE"),
        "429": reply(429, "CAPACITY_EXCEEDED", str(retry_after)),
    }[stage]
    first = start(ScriptedHttpClient(initial))
    if has_attempt:
        await dispatch_one(first)
    saved = await state(ecs)
    assert saved["frozen"]["route"]["version"] in ("3", "v1")
    assert saved["status"] == "PENDING"
    assert saved["attempts"] == has_attempt
    assert saved["not_received"] == has_attempt
    if stage == "429":
        assert saved["next"] == clock() + timedelta(seconds=retry_after)
    elif has_attempt:
        assert saved["next"] > clock()
    await first.aclose()
    assert await first.tick() == 0
    assert await state(ecs) == saved

    if has_attempt:
        clock.advance(seconds=(saved["next"] - clock()).total_seconds() / 2)

    # Current registration policy is irrelevant to the original v3 obligation.
    second_client = ScriptedHttpClient(reply())
    second = start(second_client)
    assert await state(ecs) == saved
    if expired:
        remaining = (saved["frozen"]["expires_at"].replace(tzinfo=UTC) - clock()).total_seconds()
        clock.advance(seconds=max(remaining, 0) + 1)
        await wait_for_expiry(ecs, second)
    else:
        if has_attempt:
            assert await second.tick() == 0
            assert await state(ecs) == saved
            clock.advance(seconds=(saved["next"] - clock()).total_seconds())
        await dispatch_one(second)
    current = await state(ecs)
    assert current["frozen"] == saved["frozen"]
    assert current["evidence"][: len(saved["evidence"])] == saved["evidence"]
    assert current["not_received"] == saved["not_received"]
    assert current["status"] == ("EXPIRED" if expired else "DONE")
    # The existing lifecycle counts terminal expiry as a transition too.
    assert current["attempts"] == saved["attempts"] + 1
    assert len(second_client.requests) == (not expired)
    if not expired:
        assert second_client.requests[0].body == saved["frozen"]["request_raw"]
    assert await first.tick() == 0


@pytest.mark.parametrize("restart", [600], indirect=True)
@pytest.mark.parametrize("restart_sender", [False, True], ids=["resume", "restart"])
async def test_expiry_survives_maintenance_timeout(ecs, restart, restart_sender, monkeypatch):
    clock, start = restart
    saved = await state(ecs)
    remaining = (saved["frozen"]["expires_at"].replace(tzinfo=UTC) - clock()).total_seconds()
    clock.advance(seconds=remaining + 1)
    client = ScriptedHttpClient(reply())
    sender = start(client)
    apply_transition = EcsLifecycle.apply_transition
    maintenance_timeout = asyncio.timeout(None)
    interrupted = asyncio.Event()

    async def interrupted_transition(self, session, context, claim, outcome):
        await apply_transition(self, session, context, claim, outcome)
        await session.flush()
        # Expire the real timeout after SQL writes, independent of query latency.
        maintenance_timeout.reschedule(asyncio.get_running_loop().time())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "asyncio", SimpleNamespace(timeout=lambda _: maintenance_timeout))
        patch.setattr(EcsLifecycle, "apply_transition", interrupted_transition)
        async with asyncio.timeout(5):
            assert await sender.store.maintain_snapshots(1, ecs.targets, time.monotonic() + 30) == 0

    assert interrupted.is_set()
    assert await state(ecs) == saved
    assert not client.requests
    if restart_sender:
        await sender.aclose()
        sender = start(client)
    await wait_for_expiry(ecs, sender)
    current = await state(ecs)
    assert current["frozen"] == saved["frozen"]
    assert current["status"] == "EXPIRED"
    assert current["attempts"] == saved["attempts"] + 1
    assert current["evidence"] == saved["evidence"]
    assert current["command_status"] == current["delivery_status"] == "EXPIRED"
    assert current["not_received"] == saved["not_received"]
    assert not client.requests
    assert await start(client).tick() == 0
    assert await state(ecs) == current


@pytest.mark.parametrize(
    "outcome",
    [
        reply(429, "CAPACITY_EXCEEDED"),
        reply(429, "CAPACITY_EXCEEDED", "invalid"),
        reply(429, "CAPACITY_EXCEEDED", "3601"),
        HttpResult(DeliveryState.DELIVERY_UNKNOWN, None, None, (), None),
    ],
    ids=["missing-retry-after", "invalid-retry-after", "excess-retry-after", "unknown"],
)
async def test_restart_never_unholds_uncertain_or_contract_paused_command(ecs, restart, outcome):
    clock, start = restart
    first = start(ScriptedHttpClient(outcome))
    await dispatch_one(first)
    saved = await state(ecs)
    assert saved["status"] == "HELD"
    await first.aclose()
    client = ScriptedHttpClient(reply())
    second = start(client)
    clock.advance(seconds=301)
    assert await second.tick() == 0
    assert not client.requests
    assert await state(ecs) == saved


async def test_stopping_one_sender_leaves_another_instance_able_to_dispatch(ecs, restart):
    _, start = restart
    first = start(ScriptedHttpClient(reply()))
    client = ScriptedHttpClient(reply())
    second = start(client)
    await first.aclose()
    assert await first.tick() == 0
    await dispatch_one(second)
    assert len(client.requests) == 1
    await second.aclose()
    assert await first.tick() == await second.tick() == 0
