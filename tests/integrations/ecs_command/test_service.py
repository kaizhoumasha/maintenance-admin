import pytest
from sqlalchemy import func, select

from app.models import ReliableMessage


async def create(ecs, session, key="call", qty=1):
    return await ecs.service.create(
        session,
        owner=ecs.owner,
        call_key=key,
        device_code="D1",
        task_type="MOVE",
        timeout_ms=30000,
        params={"qty": qty},
    )


async def test_duplicate_preserves_route_identity_and_bytes(ecs):
    async with ecs.factory.begin() as session:
        first = await create(ecs, session)
    ecs.routes.origin = "http://changed.test"
    async with ecs.factory.begin() as session:
        second = await create(ecs, session)
        assert second.command_code == first.command_code
        assert ecs.routes.calls == 1
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 1
        with pytest.raises(ValueError):
            await create(ecs, session, qty=2)


async def test_outer_rollback_removes_command_and_obligation(ecs):
    async with ecs.factory() as session:
        await create(ecs, session)
        await session.rollback()
    async with ecs.factory() as session:
        assert await ecs.service.get_by_call(session, owner=ecs.owner, call_key="call") is None
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_queries_are_owner_scoped(ecs):
    from app.integrations.ecs_command.service import CommandOwner

    async with ecs.factory.begin() as session:
        first = await create(ecs, session)
    async with ecs.factory() as session:
        assert (
            await ecs.service.get_command(
                session, owner=CommandOwner("other"), command_code=first.command_code
            )
            is None
        )


async def test_timeout_must_match_frozen_annex(ecs):
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError):
            await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key="bad-timeout",
                device_code="D1",
                task_type="MOVE",
                timeout_ms=999999,
                params={"qty": 1},
            )


async def test_command_and_message_share_exact_sending_deadline(ecs):
    from app.models import EcsCommand

    async with ecs.factory.begin() as session:
        await create(ecs, session)
    async with ecs.factory() as session:
        command = await session.scalar(select(EcsCommand))
        message = await session.scalar(select(ReliableMessage))
        assert command.expires_at == message.expires_at
