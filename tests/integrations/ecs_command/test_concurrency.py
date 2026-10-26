import asyncio
import json
import time

import pytest
from sqlalchemy import func, select, text

from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL
from app.integrations.ecs_command.result import receive_ecs_result
from app.models import EcsCommand, EcsResultEvidence, ReliableMessage
from app.reliable.store import MessageStore


async def create(ecs, session):
    return await ecs.service.create(
        session,
        owner=ecs.owner,
        call_key="call",
        device_code="D1",
        task_type="MOVE",
        timeout_ms=30000,
        params={"qty": 1},
    )


async def test_concurrent_create_and_result_apply_once(ecs):
    if ecs.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrency")

    async def new():
        async with ecs.factory.begin() as session:
            return await create(ecs, session)

    first, second = await asyncio.gather(new(), new())
    assert first.command_code == second.command_code
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    await store.before_send(claim, ecs.targets["ecs"])
    raw = json.dumps(
        dict(
            command_code=first.command_code,
            device_code="D1",
            result="SUCCESS",
            finish_time=1,
            data={"qty": 1},
        )
    ).encode()

    async def receive():
        async with ecs.factory.begin() as session:
            return await receive_ecs_result(session, raw, ecs.service)

    a, b = await asyncio.gather(receive(), receive())
    assert a.status_code == b.status_code == 200 and a.body == b.body
    async with ecs.factory() as session:
        assert await session.scalar(select(func.count()).select_from(EcsResultEvidence)) == 1
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1


async def test_projection_failure_rolls_back_both_records_and_maintenance_continues(ecs, caplog):
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    async with ecs.factory.begin() as session:
        await create(ecs, session)

    class Broken(EcsLifecycle):
        async def apply_transition(self, *args):
            raise RuntimeError("injected projection failure")

    store = MessageStore(ecs.factory, clock=clock, lifecycles={PROTOCOL: Broken(ecs.service)})
    clock.advance(seconds=301)
    await store.maintain_snapshots(100, ecs.targets, time.monotonic() + 0.2)
    records = [r for r in caplog.records if "snapshot maintenance unavailable" in r.getMessage()]
    assert records and records[0].component == "reliable"
    assert records[0].phase == "maintain_snapshots" and records[0].message_id
    assert "origin=" in records[0].getMessage() and "Traceback" in records[0].getMessage()
    assert "injected projection failure" not in caplog.text
    async with ecs.factory() as session:
        assert (await session.scalar(select(ReliableMessage))).status == "PENDING"
        assert (await session.scalar(select(EcsCommand))).status == "QUEUED"
    store.snapshots.lifecycles = {PROTOCOL: EcsLifecycle(ecs.service)}
    await store.maintain_snapshots(100, ecs.targets, time.monotonic() + 0.2)
    await store.maintain_snapshots(100, ecs.targets, time.monotonic() + 0.2)
    async with ecs.factory() as session:
        assert (await session.scalar(select(ReliableMessage))).status == "EXPIRED"
        assert (await session.scalar(select(EcsCommand))).status == "EXPIRED"


async def test_table_lock_cannot_block_maintenance_past_round_deadline(ecs):
    if ecs.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL table locks")
    async with ecs.factory.begin() as session:
        await create(ecs, session)
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    async with ecs.factory.begin() as blocker:
        await blocker.execute(text("LOCK TABLE ecs_command IN ACCESS EXCLUSIVE MODE"))
        started = time.monotonic()
        await store.maintain_snapshots(100, ecs.targets, started + 0.1)
        assert time.monotonic() - started < 0.8
    async with ecs.factory() as session:
        assert (await session.scalar(select(ReliableMessage))).status == "PENDING"


async def test_missing_send_assembly_pauses_without_losing_maintenance_port(ecs):
    async with ecs.factory.begin() as session:
        await create(ecs, session)
    store = MessageStore(
        ecs.factory,
        lifecycles={PROTOCOL: EcsLifecycle(ecs.service)},
        served_snapshot_protocols=set(),
    )
    await store.maintain_snapshots(100, ecs.targets, time.monotonic() + 0.3)
    async with ecs.factory() as session:
        message = await session.scalar(select(ReliableMessage))
        command = await session.scalar(select(EcsCommand))
        assert message.status == "HELD" and message.pause_reason == "ASSEMBLY_UNAVAILABLE"
        assert not message.delivery_uncertain and command.status == "RECONCILING"


@pytest.mark.parametrize("corrupt", [False, True])
async def test_broken_snapshot_projection_does_not_stop_static_delivery(ecs, corrupt):
    from app.http_client import DeliveryState, HttpRequest, HttpResult
    from app.reliable import MessageDraft, Outbox, Verdict, VerdictKind
    from app.reliable.config import TargetConfig
    from app.reliable.sender import Sender
    from app.reliable.testing import FakeClock

    clock = FakeClock()
    ecs.service.clock = clock
    ecs.outbox.clock = clock
    async with ecs.factory.begin() as session:
        await create(ecs, session)
    clock.advance(seconds=301)
    if corrupt:
        async with ecs.factory.begin() as session:
            row = await session.scalar(select(ReliableMessage))
            row.delivery_snapshot = {"protocol": "removed-protocol"}
            row.expires_at = None

    class Adapter:
        def build_request(self, message, target):
            return HttpRequest("POST", "/mock", body=message.request_raw)

        def classify_response(self, message, result):
            return Verdict(VerdictKind.DELIVERED, response=result.body)

    class Client:
        calls = 0

        async def send(self, request):
            self.calls += 1
            return HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 200, (), b"ok")

        async def aclose(self):
            pass

    class Broken(EcsLifecycle):
        async def apply_transition(self, *args):
            raise RuntimeError("injected projection failure")

    target = TargetConfig("old-static", "http://static.test", "script")
    targets = {**ecs.targets, target.name: target}
    adapters = {**ecs.adapters, "script": Adapter()}
    async with ecs.factory.begin() as session:
        await Outbox(targets, adapters, clock=clock).enqueue(
            session, MessageDraft(target.name, "opaque", "id", b"{}")
        )
    store = MessageStore(ecs.factory, clock=clock, lifecycles={PROTOCOL: Broken(ecs.service)})
    from app.integrations.ecs_command.clients import DynamicClients

    client = Client()
    sender = Sender(
        store,
        targets,
        adapters,
        {target.name: client},
        clock=clock,
        provider=DynamicClients(factory=lambda _: Client()),
    )
    assert await sender.tick() == 1
    await asyncio.gather(*list(sender.inflight))
    async with ecs.factory() as session:
        row = await session.scalar(
            select(ReliableMessage).where(ReliableMessage.target == target.name)
        )
        assert row.status == "DONE" and client.calls == 1
        await session.delete(row)
    await sender.aclose()


async def test_authorization_is_locked_before_waiting_for_message(ecs):
    if ecs.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL lock order")
    async with ecs.factory.begin() as session:
        await create(ecs, session)
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    authorized = asyncio.Event()
    original = ecs.routes.authorize_frozen

    async def authorize(session, route):
        authorized.set()
        return await original(session, route)

    ecs.routes.authorize_frozen = authorize
    async with ecs.factory.begin() as blocker:
        await blocker.scalar(
            select(ReliableMessage).where(ReliableMessage.id == claim.message_id).with_for_update()
        )
        task = asyncio.create_task(store.before_send(claim, ecs.targets["ecs"]))
        try:
            await asyncio.wait_for(authorized.wait(), 0.5)
        finally:
            await blocker.rollback()
            await task


@pytest.mark.parametrize("first", ["revoke", "gate"])
async def test_persistent_route_revocation_serializes_with_send_gate(
    reference,
    wait_for_lock,
    monkeypatch,
    first,
):
    from sqlalchemy import update

    ecs = reference.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL original authorization row locking")
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    if first == "revoke":
        async with ecs.factory.begin() as blocker:
            await blocker.execute(update(reference.routes).values(revoked=True))
            pending = asyncio.create_task(store.before_send(claim, ecs.targets["ecs"]))
            try:
                await wait_for_lock(pending)
                assert not pending.done()
            finally:
                await blocker.commit()
                permitted = await asyncio.wait_for(pending, 5)
        assert not permitted
    else:
        authorized, release = asyncio.Event(), asyncio.Event()
        original = reference.authorize_frozen

        async def hold_authorization(session, route):
            answer = await original(session, route)
            authorized.set()
            await release.wait()
            return answer

        monkeypatch.setattr(reference, "authorize_frozen", hold_authorization)
        gate = asyncio.create_task(store.before_send(claim, ecs.targets["ecs"]))
        revocation = None
        try:
            await asyncio.wait_for(authorized.wait(), 5)

            async def revoke():
                async with ecs.factory.begin() as session:
                    await session.execute(update(reference.routes).values(revoked=True))

            revocation = asyncio.create_task(revoke())
            await wait_for_lock(revocation)
            assert not revocation.done()
        finally:
            release.set()
            permitted = await asyncio.wait_for(gate, 5)
            if revocation:
                await asyncio.wait_for(revocation, 5)
        assert permitted
    async with ecs.factory() as session:
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == ("RECONCILING" if first == "revoke" else "DISPATCHING")
        assert view.hold_reason == ("AUTHORIZATION_REVOKED" if first == "revoke" else None)
        assert view.result is None


async def test_missing_original_authorization_does_not_allow_dispatch(reference):
    from sqlalchemy import delete

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
        await session.execute(delete(reference.routes))
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    assert not await store.before_send(claim, ecs.targets["ecs"])
    async with ecs.factory() as session:
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.status == "RECONCILING" and view.result is None
