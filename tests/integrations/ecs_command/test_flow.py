import json

import pytest
from sqlalchemy import select

from app.http_client import DeliveryState, HttpResult
from app.models import EcsCommand, ReliableMessage


async def prepare(ecs, key="call"):
    async with ecs.factory.begin() as session:
        return await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key=key,
            device_code="D1",
            task_type="MOVE",
            timeout_ms=30000,
            params={"qty": 1},
        )


def response(code=200, message="ACK"):
    return HttpResult(
        DeliveryState.HTTP_RESPONSE_COMPLETE,
        None,
        code,
        (("content-type", "application/json"),),
        json.dumps(dict(code=code, message=message)).encode(),
    )


def result_body(code):
    return json.dumps(
        dict(command_code=code, device_code="D1", result="SUCCESS", finish_time=1, data={"qty": 1})
    ).encode()


async def make_store(ecs):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.reliable.store import MessageStore

    return MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})


async def test_first_result_before_http_response_applies_once(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory.begin() as session:
        first = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert first.status_code == 200
    async with ecs.factory.begin() as session:
        again = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert again.body == first.body
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "SUCCEEDED" and not view.ack_received
        row = await session.scalar(select(ReliableMessage))
        assert row.status == "DONE" and row.response_raw is None


async def test_early_result_revokes_claim_before_network(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    async with ecs.factory.begin() as session:
        reply = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert reply.status_code == 409
    assert not await store.before_send(claim, ecs.targets["ecs"])


async def test_unknown_delivery_is_held_and_restart_does_not_retry(ecs):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.reliable.delivery import send_one

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]

    class Client:
        async def send(self, request):
            return HttpResult(DeliveryState.DELIVERY_UNKNOWN, None, None, (), None)

        async def aclose(self):
            pass

    clients = DynamicClients(factory=lambda cfg: Client())
    await send_one(
        claim,
        store=store,
        targets=ecs.targets,
        adapters=ecs.adapters,
        clients={},
        provider=clients,
        clock=ecs.service.clock,
        uniform=lambda a, b: b,
    )
    async with ecs.factory() as session:
        row = await session.scalar(select(ReliableMessage))
        assert row.status == "HELD" and row.delivery_uncertain
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "RECONCILING"
    assert await store.claim(1, ecs.targets) == ()
    await clients.aclose()


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (200, "ACK", "DONE"),
        (400, "INVALID_ENVELOPE", "FAILED"),
        (409, "IDEMPOTENCY_CONFLICT", "HELD"),
        (429, "CAPACITY_EXCEEDED", "HELD"),
        (503, "TEMPORARILY_UNAVAILABLE", "PENDING"),
        (500, "TEMPORARILY_UNAVAILABLE", "HELD"),
    ],
)
async def test_sender_matrix_and_command_projection(ecs, code, message, expected):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.reliable.delivery import send_one

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    seen = []

    class Client:
        async def send(self, request):
            seen.append(request.body)
            return response(code, message)

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
        clock=ecs.service.clock,
        uniform=lambda a, b: b,
    )
    async with ecs.factory() as session:
        row = await session.scalar(select(ReliableMessage))
        assert row.status == expected
        assert seen == [row.request_raw]
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.delivery_status == expected
        assert (
            view.status
            == {
                "DONE": "WAITING_RESULT",
                "FAILED": "REJECTED",
                "HELD": "RECONCILING",
                "PENDING": "QUEUED",
            }[expected]
        )
        assert view.result is None
    await provider.aclose()


async def test_known_not_received_conflicts_but_new_inflight_attempt_accepts(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus, Verdict, VerdictKind

    command = await prepare(ecs)
    store = await make_store(ecs)
    first = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(first, ecs.targets["ecs"])
    await store.finish(
        first,
        AttemptOutcome(
            MessageStatus.PENDING,
            result=response(503, "TEMPORARILY_UNAVAILABLE"),
            verdict=Verdict(VerdictKind.NOT_RECEIVED),
            next_attempt_at=ecs.service.clock(),
        ),
    )
    second = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(second, ecs.targets["ecs"])
    async with ecs.factory.begin() as session:
        assert (
            await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        ).status_code == 200


async def test_definite_nonacceptance_result_is_persistently_conflicting(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus, Verdict, VerdictKind

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.PENDING,
            result=response(503, "TEMPORARILY_UNAVAILABLE"),
            verdict=Verdict(VerdictKind.NOT_RECEIVED),
            next_attempt_at=ecs.service.clock(),
        ),
    )
    async with ecs.factory.begin() as session:
        first = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert first.status_code == 409
    async with ecs.factory.begin() as session:
        assert (
            await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        ).body == first.body
        assert (await session.scalar(select(ReliableMessage))).status == "HELD"


async def test_result_and_late_rejection_keep_result_and_evidence(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result
    from app.models import EcsSendEvidence
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import FailReason, MessageStatus, Verdict, VerdictKind

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory.begin() as session:
        await receive_ecs_result(session, result_body(command.command_code), ecs.service)
    assert not await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.FAILED,
            FailReason.REJECTED,
            response(400, "INVALID_ENVELOPE"),
            Verdict(VerdictKind.PEER_TERMINAL, reason=FailReason.REJECTED),
        ),
    )
    async with ecs.factory() as session:
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "SUCCEEDED" and view.needs_reconciliation
        assert (await session.scalar(select(EcsSendEvidence))).response_raw is not None


async def test_result_survives_receipt_and_delivery_cleanup(ecs):
    from sqlalchemy import delete

    from app.integrations.ecs_command.result import receive_ecs_result
    from app.models import CallbackReceipt
    from app.reliable.store import AttemptOutcome
    from app.reliable.types import MessageStatus, Verdict, VerdictKind

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    result = response()
    await store.finish(
        claim,
        AttemptOutcome(
            MessageStatus.DONE,
            result=result,
            verdict=Verdict(VerdictKind.DELIVERED, response=result.body),
        ),
    )
    async with ecs.factory.begin() as session:
        await session.execute(delete(ReliableMessage))
    async with ecs.factory.begin() as session:
        first = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert first.status_code == 200
    async with ecs.factory.begin() as session:
        await session.execute(delete(CallbackReceipt))
    ecs.service.annexes = {}
    async with ecs.factory.begin() as session:
        assert (
            await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        ).body == first.body
        assert (
            await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key="call",
                device_code="D1",
                task_type="MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )
        ).command_code == command.command_code
        assert await session.scalar(select(ReliableMessage)) is None


async def test_gate_does_not_renew_lease_expired_during_authorization(ecs):
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    await prepare(ecs)
    store = await make_store(ecs)
    store.clock = clock
    claim = (await store.claim(1, ecs.targets))[0]
    original = ecs.routes.authorize_frozen

    async def slow_authorize(session, route):
        clock.advance(seconds=30)
        return await original(session, route)

    ecs.routes.authorize_frozen = slow_authorize
    assert not await store.before_send(claim, ecs.targets["ecs"])


async def test_sender_http_and_result_http_complete_before_ack(ecs):
    import asyncio

    import httpx
    from fastapi import FastAPI

    from app.controllers.ecs_result import router
    from app.http_client import AsyncHttpClient
    from app.integrations.ecs_command.clients import DynamicClients
    from app.reliable.sender import Sender

    command = await prepare(ecs)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.callback_session_factory = ecs.factory
    app.state.ecs_commands = ecs.service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wes.test"
    ) as receiver:

        async def handle(request):
            assert request.url.path == "/api/v1/device/command"
            body = json.loads(request.content)
            assert body["command_code"] == command.command_code and body["priority"] == 1
            result = await receiver.post(
                "/api/v1/callback/result",
                content=result_body(command.command_code),
                headers={"content-type": "application/json"},
            )
            assert result.status_code == 200, result.text
            return httpx.Response(200, json={"code": 200, "message": "ACK"})

        provider = DynamicClients(
            factory=lambda cfg: AsyncHttpClient(cfg, transport=httpx.MockTransport(handle))
        )
        store = await make_store(ecs)
        sender = Sender(store, ecs.targets, ecs.adapters, {}, provider=provider)
        assert await sender.tick() == 1
        await asyncio.gather(*list(sender.inflight))
        async with ecs.factory() as session:
            view = await ecs.service.get_command(
                session, owner=ecs.owner, command_code=command.command_code
            )
            assert view.status == "SUCCEEDED" and view.ack_received
        await sender.aclose()


async def test_public_pause_keeps_projection_and_blocks_result(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory.begin() as session:
        assert await ecs.outbox.hold(
            session, "ecs", "ecs.command", command.command_code, reason="AUTHORIZATION_REVOKED"
        )
    async with ecs.factory.begin() as session:
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "RECONCILING" and view.hold_reason == "AUTHORIZATION_REVOKED"
        assert (
            await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        ).status_code == 409


async def test_bare_snapshot_settle_does_not_invent_command_acceptance(ecs):
    command = await prepare(ecs)
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.outbox.settle(session, "ecs", "ecs.command", command.command_code)
    async with ecs.factory() as session:
        assert (await session.scalar(select(ReliableMessage))).status == "PENDING"
        assert (await session.scalar(select(EcsCommand))).status == "QUEUED"


async def test_snapshot_mutation_without_lifecycle_is_rejected(ecs):
    from app.reliable import Outbox

    command = await prepare(ecs)
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await Outbox({}, {}).hold(
                session, "ecs", "ecs.command", command.command_code, reason="AUTHORIZATION_REVOKED"
            )
    async with ecs.factory() as session:
        assert (await session.scalar(select(ReliableMessage))).status == "PENDING"
        assert (await session.scalar(select(EcsCommand))).status == "QUEUED"


async def test_revoked_old_origin_does_not_block_new_independent_command(ecs):
    from app.integrations.ecs_command.routing import RouteAuthorization

    first = await prepare(ecs, "old")
    ecs.routes.origin = "http://new.test"
    second = await prepare(ecs, "new")

    async def authorize(session, route):
        return (
            RouteAuthorization.REVOKED
            if route["origin"] == "http://ecs.test"
            else RouteAuthorization.ALLOW
        )

    ecs.routes.authorize_frozen = authorize
    store = await make_store(ecs)
    old = (await store.claim(1, ecs.targets))[0]
    assert not await store.before_send(old, ecs.targets["ecs"])
    new = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(new, ecs.targets["ecs"])
    async with ecs.factory() as session:
        old_view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=first.command_code
        )
        new_view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=second.command_code
        )
        assert old_view.hold_reason == "AUTHORIZATION_REVOKED"
        assert new_view.status == "DISPATCHING"


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (200, "ACK", "DONE"),
        (503, "TEMPORARILY_UNAVAILABLE", "EXPIRED"),
        (500, "TEMPORARILY_UNAVAILABLE", "HELD"),
    ],
)
async def test_response_after_sending_deadline_is_not_discarded(ecs, code, message, expected):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.reliable.delivery import send_one
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    command = await prepare(ecs)
    store = await make_store(ecs)
    store.clock = clock
    claim = (await store.claim(1, ecs.targets))[0]

    class Client:
        async def send(self, request):
            clock.advance(seconds=301)
            return response(code, message)

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
        uniform=lambda a, b: b,
    )
    async with ecs.factory() as session:
        row = await session.scalar(select(ReliableMessage))
        assert row.status == expected and row.last_http_status == code
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert (
            view.status
            == {"DONE": "WAITING_RESULT", "EXPIRED": "EXPIRED", "HELD": "RECONCILING"}[expected]
        )
    await provider.aclose()


async def test_retry_after_is_persisted_without_early_retry(ecs):
    from app.integrations.ecs_command.clients import DynamicClients
    from app.reliable.delivery import send_one
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    await prepare(ecs)
    store = await make_store(ecs)
    store.clock = clock
    claim = (await store.claim(1, ecs.targets))[0]

    class Client:
        async def send(self, request):
            r = response(429, "CAPACITY_EXCEEDED")
            return HttpResult(
                r.delivery_state, None, r.status_code, (*r.headers, ("Retry-After", "120")), r.body
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
        uniform=lambda a, b: b,
    )
    clock.advance(seconds=119)
    assert await store.claim(1, ecs.targets) == ()
    clock.advance(seconds=1)
    assert len(await store.claim(1, ecs.targets)) == 1
    await provider.aclose()


async def test_http_commit_failure_rolls_back_result_receipt_and_settlement(ecs):
    from contextlib import asynccontextmanager

    import httpx
    from fastapi import FastAPI
    from sqlalchemy import event, func

    from app.controllers.ecs_result import router
    from app.models import CallbackReceipt, EcsResultEvidence

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])

    @asynccontextmanager
    async def failing_factory():
        async with ecs.factory() as session:

            def fail_outer(sync_session):
                if not sync_session.in_nested_transaction():
                    raise RuntimeError("injected commit failure")

            event.listen(session.sync_session, "before_commit", fail_outer)
            yield session

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.ecs_commands = ecs.service
    app.state.callback_session_factory = failing_factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wes.test"
    ) as client:
        result = await client.post(
            "/api/v1/callback/result", content=result_body(command.command_code)
        )
        assert result.status_code == 503
    async with ecs.factory() as session:
        for model in (EcsResultEvidence, CallbackReceipt):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (await session.scalar(select(ReliableMessage))).status == "SENDING"
        assert (await session.scalar(select(EcsCommand))).status == "DISPATCHING"


@pytest.mark.parametrize("interruption", ["release", "expired-lease"])
async def test_interrupted_snapshot_lease_holds_until_matching_result(ecs, interruption):
    import time

    from app.integrations.ecs_command.result import receive_ecs_result
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    command = await prepare(ecs)
    store = await make_store(ecs)
    store.clock = clock
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    clock.advance(seconds=30)
    if interruption == "release":
        assert await store.release(claim, ecs.targets)
    else:
        await store.maintain_snapshots(100, ecs.targets, time.monotonic() + 0.3)
    async with ecs.factory() as session:
        message = await session.scalar(select(ReliableMessage))
        assert message.status == "HELD" and message.delivery_uncertain
        assert message.pause_reason == "LEASE_INTERRUPTED" and message.next_attempt_at is None
    restarted = await make_store(ecs)
    restarted.clock = clock
    assert await restarted.claim(1, ecs.targets) == ()
    async with ecs.factory.begin() as session:
        result = await receive_ecs_result(session, result_body(command.command_code), ecs.service)
        assert result.status_code == 200
        await session.flush()
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "SUCCEEDED" and not view.ack_received


async def test_failed_device_result_is_applied_and_queryable_without_ack(ecs):
    from app.integrations.ecs_command.result import receive_ecs_result

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])
    core = dict(
        command_code=command.command_code,
        device_code="D1",
        result="FAILED",
        finish_time=1,
        data={"qty": 1},
        error_detail={"code": "DEVICE_FAILURE", "msg": "movement failed"},
    )
    raw = json.dumps(core).encode()
    async with ecs.factory.begin() as session:
        first = await receive_ecs_result(session, raw, ecs.service)
        assert first.status_code == 200
    async with ecs.factory.begin() as session:
        replay = await receive_ecs_result(session, raw, ecs.service)
        assert replay.body == first.body
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "FAILED" and view.result == core
        assert view.delivery_status == "DONE" and not view.ack_received
        message = await session.scalar(select(ReliableMessage))
        assert message.status == "DONE" and message.response_raw is None


async def test_missing_original_annex_returns_retryable_503_without_result_evidence(ecs):
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import func

    from app.controllers.ecs_result import router
    from app.models import CallbackReceipt, EcsResultEvidence

    command = await prepare(ecs)
    store = await make_store(ecs)
    claim = (await store.claim(1, ecs.targets))[0]
    assert await store.before_send(claim, ecs.targets["ecs"])
    annex = ecs.service.annexes.pop(("test", "1"))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.ecs_commands = ecs.service
    app.state.callback_session_factory = ecs.factory
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://wes.test"
    ) as client:
        response = await client.post(
            "/api/v1/callback/result", content=result_body(command.command_code)
        )
        assert response.status_code == 503
        assert response.json() == {"code": 503, "message": "TEMPORARILY_UNAVAILABLE"}
        async with ecs.factory() as session:
            for model in (EcsResultEvidence, CallbackReceipt):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
            assert (await session.scalar(select(ReliableMessage))).status == "SENDING"
            assert (await session.scalar(select(EcsCommand))).status == "DISPATCHING"
        ecs.service.annexes["test", "1"] = annex
        retried = await client.post(
            "/api/v1/callback/result", content=result_body(command.command_code)
        )
        assert retried.status_code == 200


@pytest.mark.parametrize("device_result", ["SUCCESS", "FAILED"])
@pytest.mark.parametrize("result_timing", ["before_ack", "after_ack"])
async def test_loopback_command_result_and_reference_consumption(
    reference,
    loopback_server,
    device_result,
    result_timing,
):
    import asyncio

    import httpx
    from fastapi import FastAPI, Request
    from sqlalchemy import func, update

    from app.controllers.ecs_result import router
    from app.integrations.ecs_command.clients import DynamicClients
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.models import EcsResultEvidence
    from app.reliable.sender import Sender
    from app.reliable.store import MessageStore

    ecs = reference.ecs
    receiver = FastAPI()
    receiver.include_router(router, prefix="/api/v1")
    receiver.state.callback_session_factory = ecs.factory
    receiver.state.ecs_commands = ecs.service
    peer = FastAPI()
    requests = []
    received_results = []

    async with loopback_server(receiver) as callback_origin:
        async with httpx.AsyncClient(base_url=callback_origin, trust_env=False) as client:

            async def report(code):
                body = reference.result_body(code, device_result)
                answer = await client.post("/api/v1/callback/result", json=body)
                received_results.append(answer.json())
                assert answer.status_code == 200

            @peer.post("/api/v1/device/command")
            async def command_endpoint(request: Request):
                raw = await request.body()
                requests.append(raw)
                if result_timing == "before_ack":
                    await report(json.loads(raw)["command_code"])
                return {"code": 200, "message": "ACK"}

            async with loopback_server(peer) as origin:
                async with ecs.factory.begin() as session:
                    await session.execute(update(reference.routes).values(origin=origin))
                    command = await reference.register(session, action_id=1)
                store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
                sender = Sender(store, ecs.targets, ecs.adapters, {}, provider=DynamicClients())
                try:
                    assert await sender.tick() == 1
                    async with asyncio.timeout(10):
                        await asyncio.gather(*list(sender.inflight))
                    async with ecs.factory.begin() as session:
                        view = await ecs.service.get_command(
                            session,
                            owner=ecs.owner,
                            command_code=command.command_code,
                        )
                        assert view.ack_received and view.delivery_status == "DONE"
                        if result_timing == "after_ack":
                            assert view.result is None and view.status == "WAITING_RESULT"
                            assert not await reference.consume(session, action_id=1)
                            assert (await reference.state(session))["state"] == "WAITING_RESULT"
                    await report(command.command_code)
                    async with ecs.factory.begin() as session:
                        assert await reference.consume(session, action_id=1)
                    async with ecs.factory.begin() as session:
                        assert not await reference.consume(session, action_id=1)
                        state = await reference.state(session)
                        assert state["state"] == (
                            "SUCCEEDED" if device_result == "SUCCESS" else "FAILED"
                        )
                        view = await ecs.service.get_by_call(
                            session, owner=ecs.owner, call_key="action:1"
                        )
                        assert view.result["result"] == device_result
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(reference.actions)
                                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
                            )
                            == 1
                        )
                        assert (
                            await session.scalar(
                                select(func.count()).select_from(EcsResultEvidence)
                            )
                            == 1
                        )
                        row = await session.scalar(select(EcsCommand))
                        assert requests == [row.request_raw]
                        assert row.route["origin"] == origin
                    assert received_results and all(
                        r == {"code": 200, "message": "ACK"} for r in received_results
                    )
                finally:
                    await sender.aclose()
