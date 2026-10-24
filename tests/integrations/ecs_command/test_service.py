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


async def test_pending_route_keeps_active_route_and_revocation_only_fences_old_call(reference):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.models import EcsCommand
    from app.reliable.store import MessageStore

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        first = await reference.register(session)
    async with ecs.factory.begin() as session:
        await reference.add_route(session, "B", effective=False)
    async with ecs.factory.begin() as session:
        pending = await reference.register(session, key="pending")
        assert (await session.get(EcsCommand, pending.command_code)).route["route_id"] == "A"
    async with ecs.factory.begin() as session:
        await reference.switch_route(session)
    async with ecs.factory.begin() as session:
        again = await reference.register(session)
        second = await reference.register(session, key="new")
        assert again.command_code == first.command_code
        old = await session.get(EcsCommand, first.command_code)
        new = await session.get(EcsCommand, second.command_code)
        assert old.route["route_id"] == "A" and new.route["route_id"] == "B"
        frozen = old.request_raw
    store = MessageStore(ecs.factory, lifecycles={PROTOCOL: EcsLifecycle(ecs.service)})
    claims = await store.claim(10, ecs.targets)
    permitted = {}
    for claim in claims:
        permitted[claim.message.operation_id] = bool(
            await store.before_send(claim, ecs.targets["ecs"])
        )
    assert not permitted[first.command_code] and not permitted[pending.command_code]
    assert permitted[second.command_code]
    async with ecs.factory.begin() as session:
        assert (await reference.register(session)).command_code == first.command_code
        assert (await session.get(EcsCommand, first.command_code)).request_raw == frozen


@pytest.mark.parametrize("failure", ["reference_write", "enqueue", "outer_rollback"])
async def test_reference_registration_and_outbox_rollback_together(reference, monkeypatch, failure):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from app.models import EcsCommand

    ecs = reference.ecs
    async with ecs.factory() as session:
        if failure == "enqueue":

            async def fail(*args, **kwargs):
                raise RuntimeError("enqueue failed")

            monkeypatch.setattr(ecs.outbox, "enqueue", fail)
        if failure == "outer_rollback":
            await reference.register(session)
        else:
            with pytest.raises(RuntimeError if failure == "enqueue" else IntegrityError):
                await reference.register(session)
                await session.execute(update(reference.business).values(state="INVALID"))
        await session.rollback()
    async with ecs.factory() as session:
        for table in (reference.calls, EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        assert (await reference.state(session))["state"] == "READY"


@pytest.mark.parametrize("case", ["missing", "ambiguous", "action"])
async def test_no_single_authorized_route_or_action_has_no_side_effects(reference, case):
    from sqlalchemy import delete

    from app.models import EcsCommand

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        if case == "missing":
            await session.execute(delete(reference.routes))
        elif case == "ambiguous":
            await reference.add_route(session, "B", effective=True)
    async with ecs.factory.begin() as session:
        with pytest.raises(ValueError, match="authorized effective route"):
            await ecs.service.create(
                session,
                owner=ecs.owner,
                call_key="bad",
                device_code="D1",
                task_type="OTHER" if case == "action" else "MOVE",
                timeout_ms=30000,
                params={"qty": 1},
            )
        for table in (reference.calls, EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_concurrent_reference_registration_preserves_original_mapping(reference):
    import asyncio

    from app.models import EcsCommand

    ecs = reference.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL reference registration locking")

    async def register():
        async with ecs.factory.begin() as session:
            return (await reference.register(session)).command_code

    codes = await asyncio.gather(register(), register())
    assert codes[0] == codes[1]
    async with ecs.factory() as session:
        for table in (reference.calls, EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
        call = (await session.execute(select(reference.calls))).mappings().one()
        assert call["binding"] == "D1" and call["version"] == 1


@pytest.mark.parametrize("change", ["cancel", "rebind", "SUCCESS", "FAILED"])
async def test_new_reference_intent_requires_current_permission_but_replay_keeps_identity(
    reference,
    change,
):
    from app.models import EcsCommand

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        original = await reference.register(session)
    if change in ("SUCCESS", "FAILED"):
        await reference.apply_result(original.command_code, change)
        async with ecs.factory.begin() as session:
            assert await reference.consume(session, original.command_code)
    else:
        async with ecs.factory.begin() as session:
            await reference.change(session, change)
    async with ecs.factory.begin() as session:
        assert (await reference.register(session)).command_code == original.command_code
        with pytest.raises(ValueError, match="current reference permission"):
            await reference.register(session, key="new-independent-intent")
        for table in (reference.calls, EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
        mapping = (await session.execute(select(reference.calls))).mappings().one()
        assert mapping["binding"] == "D1" and mapping["version"] == 1


async def test_new_authorized_reference_intent_uses_current_version(reference):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        original = await reference.register(session)
        await reference.change(session, "version")
        current = await reference.register(session, key="new-independent-intent")
        assert current.command_code != original.command_code
        mappings = (await session.execute(select(reference.calls))).mappings().all()
        assert {m["call_key"]: m["version"] for m in mappings} == {
            "call": 1,
            "new-independent-intent": 2,
        }
