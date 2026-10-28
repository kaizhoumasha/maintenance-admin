import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import delete, select

from app.integrations.ecs_command import compile_commands
from app.integrations.ecs_command.lifecycle import EcsLifecycle
from app.integrations.ecs_command.protocol import PROTOCOL
from app.models import Device, DeviceRoute, EcsCommand, WorkLine
from app.reliable.store import MessageStore
from app.services.device_routes import DeviceRoutes
from tests.helpers import EcsMove, add_bound_device, loopback_app, simulator_sender


@pytest.fixture
async def role_device(ecs):
    from app.core.identifiers import uuid7

    code = "role-" + uuid7().hex
    async with ecs.factory.begin() as session:
        line, device, route = await add_bound_device(session, code, "http://127.0.0.1:45841")
        line_id, device_id, route_id = line.id, device.id, route.id
    ecs.service.routes = DeviceRoutes(
        compile_commands(EcsMove)[0],
        annexes=ecs.service.annexes,
    )
    try:
        yield SimpleNamespace(
            ecs=ecs, code=code, line_id=line_id, device_id=device_id, route_id=route_id
        )
    finally:
        async with ecs.factory.begin() as session:
            await session.execute(delete(Device).where(Device.id == device_id))
            await session.execute(delete(DeviceRoute).where(DeviceRoute.id == route_id))
            await session.execute(delete(WorkLine).where(WorkLine.id == line_id))


async def create(device, key):
    ecs = device.ecs
    async with ecs.factory.begin() as session:
        return await ecs.service.create(
            session,
            owner=ecs.owner,
            call_key=key,
            device_code=device.code,
            timeout_ms=30000,
            command=EcsMove.model_validate({"qty": 1}),
        )


async def test_configuration_commit_before_gate_stops_the_claim(role_device, wait_for_lock):
    ecs = role_device.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    command = await create(role_device, "before-gate")
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    pending = None
    try:
        async with ecs.factory.begin() as writer:
            line = await writer.scalar(
                select(WorkLine).where(WorkLine.id == role_device.line_id).with_for_update()
            )
            line.version += 1
            pending = asyncio.create_task(store.before_send(claim, ecs.targets["ecs"]))
            await wait_for_lock(pending)
            assert not pending.done()
        assert not await asyncio.wait_for(pending, 5)
        async with ecs.factory() as session:
            saved = await session.get(EcsCommand, command.command_code)
            assert saved.hold_reason == "AUTHORIZATION_REVOKED"
            assert saved.dispatch_started_at is None
    finally:
        if pending and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_gate_commit_precedes_configuration_and_later_attempt_is_blocked(
    role_device, wait_for_lock, monkeypatch
):
    from app.models import EcsSendEvidence

    ecs = role_device.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks")
    command = await create(role_device, "gate-first")
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claim = (await store.claim(1, ecs.targets))[0]
    locked, release = asyncio.Event(), asyncio.Event()
    original = ecs.service.routes.authorize_frozen

    async def pause_after_authorization(session, frozen):
        answer = await original(session, frozen)
        locked.set()
        await release.wait()
        return answer

    async def change():
        async with ecs.factory.begin() as session:
            line = await session.get(WorkLine, role_device.line_id, with_for_update=True)
            line.version += 1

    monkeypatch.setattr(ecs.service.routes, "authorize_frozen", pause_after_authorization)
    gate = asyncio.create_task(store.before_send(claim, ecs.targets["ecs"]))
    writer = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        writer = asyncio.create_task(change())
        await wait_for_lock(writer)
        assert not writer.done()
        release.set()
        assert await asyncio.wait_for(gate, 5)
        await asyncio.wait_for(writer, 5)
        assert not await store.before_send(claim, ecs.targets["ecs"])
        async with ecs.factory() as session:
            saved = await session.get(EcsCommand, command.command_code)
            assert saved.dispatch_started_at is not None
            assert saved.hold_reason == "AUTHORIZATION_REVOKED"
            attempts = list(
                await session.scalars(
                    select(EcsSendEvidence).where(
                        EcsSendEvidence.command_code == command.command_code
                    )
                )
            )
            assert len(attempts) == 1
    finally:
        release.set()
        for task in (gate, writer):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (gate, writer) if t), return_exceptions=True)


async def test_loopback_only_receives_commands_from_current_configuration(role_device):
    ecs = role_device.ecs
    peer = FastAPI()
    received = []

    @peer.post("/api/v1/device/command")
    async def command(request: Request):
        received.append(await request.json())
        return {"code": 200, "message": "ACK"}

    async with loopback_app(peer) as origin:
        async with ecs.factory.begin() as session:
            route = await session.get(DeviceRoute, role_device.route_id)
            route.ecs_origin = origin
        original = await create(role_device, "old")
        async with ecs.factory.begin() as session:
            line = await session.get(WorkLine, role_device.line_id, with_for_update=True)
            line.version += 1
        async with simulator_sender(ecs, {origin}) as sender:
            await sender.tick()
            async with asyncio.timeout(5):
                await asyncio.gather(*list(sender.inflight))
            assert received == []
            current = await create(role_device, "current")
            await sender.tick()
            async with asyncio.timeout(5):
                await asyncio.gather(*list(sender.inflight))
        assert [row["command_code"] for row in received] == [current.command_code]
        async with ecs.factory() as session:
            old = await session.get(EcsCommand, original.command_code)
            assert old.hold_reason == "AUTHORIZATION_REVOKED"
            assert (
                await ecs.service.get_by_call(session, owner=ecs.owner, call_key="old")
            ).command_code == original.command_code
