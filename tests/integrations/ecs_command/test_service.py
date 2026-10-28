import pytest
from sqlalchemy import func, select, update

from app.models import ReliableMessage
from tests.helpers import EcsMove, EcsOther


async def create(ecs, session, key="call", qty=1):
    return await ecs.service.create(
        session,
        owner=ecs.owner,
        call_key=key,
        device_code="D1",
        timeout_ms=30000,
        command=EcsMove.model_validate({"qty": qty}),
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
                timeout_ms=999999,
                command=EcsMove.model_validate({"qty": 1}),
            )


async def test_command_and_message_share_exact_sending_deadline(ecs):
    from app.models import EcsCommand

    async with ecs.factory.begin() as session:
        await create(ecs, session)
    async with ecs.factory() as session:
        command = await session.scalar(select(EcsCommand))
        message = await session.scalar(select(ReliableMessage))
        assert command.expires_at == message.expires_at


@pytest.mark.parametrize("revoked", [False, True])
async def test_pending_route_keeps_active_route_and_revocation_only_fences_old_call(
    reference, revoked
):
    from app.integrations.ecs_command.lifecycle import EcsLifecycle
    from app.integrations.ecs_command.protocol import PROTOCOL
    from app.models import EcsCommand
    from app.reliable.store import MessageStore

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        first = await reference.register(session, action_id=1)
    async with ecs.factory.begin() as session:
        await reference.add_route(session, "B", effective=False)
    async with ecs.factory.begin() as session:
        pending = await reference.register(session, action_id=2)
        assert (await session.get(EcsCommand, pending.command_code)).route["route_id"] == "A"
    async with ecs.factory.begin() as session:
        await reference.switch_route(session)
        await session.execute(
            update(reference.routes).where(reference.routes.c.id == "A").values(revoked=revoked)
        )
    async with ecs.factory.begin() as session:
        again = await reference.register(session, action_id=1)
        second = await reference.register(session, action_id=3)
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
    assert permitted[first.command_code] is (not revoked)
    assert permitted[pending.command_code] is (not revoked)
    assert permitted[second.command_code]
    async with ecs.factory.begin() as session:
        assert (await reference.register(session, action_id=1)).command_code == first.command_code
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
            await reference.register(session, action_id=1)
        else:
            with pytest.raises(RuntimeError if failure == "enqueue" else IntegrityError):
                await reference.register(session, action_id=1)
                await session.execute(update(reference.actions).values(state="INVALID"))
        await session.rollback()
    async with ecs.factory() as session:
        for table in (EcsCommand, ReliableMessage):
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
                timeout_ms=30000,
                command=(EcsOther if case == "action" else EcsMove)(qty=1),
            )
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def test_concurrent_reference_registration_preserves_original_mapping(reference):
    import asyncio

    from app.models import EcsCommand

    ecs = reference.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL reference registration locking")

    async def register():
        async with ecs.factory.begin() as session:
            return (await reference.register(session, action_id=1)).command_code

    codes = await asyncio.gather(register(), register())
    assert codes[0] == codes[1]
    async with ecs.factory() as session:
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
        call = (await session.execute(select(reference.actions))).mappings().one()
        assert call["intent_device_code"] == "D1" and call["intent_version"] == 1


@pytest.mark.parametrize("change", ["cancel", "rebind", "SUCCESS", "FAILED"])
async def test_new_reference_intent_requires_current_permission_but_replay_keeps_identity(
    reference,
    change,
):
    from app.models import EcsCommand

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        original = await reference.register(session, action_id=1)
    if change in ("SUCCESS", "FAILED"):
        await reference.apply_result(original.command_code, change)
        async with ecs.factory.begin() as session:
            assert await reference.consume(session, action_id=1)
    else:
        async with ecs.factory.begin() as session:
            await reference.change(session, action_id=1, kind=change)
    async with ecs.factory() as session:
        state = await reference.state(session)
    reference.facts["B1"] = {
        "can_register": change == "rebind",
        "device_code": state["device_code"],
        "version": state["version"],
    }
    async with ecs.factory.begin() as session:
        reads = len(reference.fact_reads)
        assert (
            await reference.register(session, action_id=1)
        ).command_code == original.command_code
        assert len(reference.fact_reads) == reads
        with pytest.raises(ValueError, match="current reference permission"):
            await reference.register(session, action_id=2)
        assert len(reference.fact_reads) == reads + 1
        assert await reference.state(session, action_id=2) is None
        for table in (EcsCommand, ReliableMessage):
            assert await session.scalar(select(func.count()).select_from(table)) == 1
        mapping = (await session.execute(select(reference.actions))).mappings().one()
        assert mapping["intent_device_code"] == "D1" and mapping["intent_version"] == 1


async def test_new_authorized_reference_intent_uses_current_version(reference):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        original = await reference.register(session, action_id=1)
        await reference.change(session, action_id=1, kind="version")
    reference.facts["B1"] = {"can_register": True, "device_code": "D1", "version": 2}
    async with ecs.factory.begin() as session:
        current = await reference.register(session, action_id=2)
        assert current.command_code != original.command_code
        rows = (await session.execute(select(reference.actions))).mappings().all()
        assert {r["id"]: r["intent_version"] for r in rows} == {1: 1, 2: 2}
        assert {r["business_key"] for r in rows} == {"B1"}
        assert (
            await reference.register(session, action_id=1)
        ).command_code == original.command_code


@pytest.mark.parametrize("fact", [None, {"can_register": False, "device_code": "D1", "version": 7}])
async def test_new_action_requires_current_business_fact(reference, fact):
    from app.models import EcsCommand

    async def current_business(session, *, business_key):
        assert business_key == "B1"
        return fact

    reference.current_business = current_business
    async with reference.ecs.factory.begin() as session:
        with pytest.raises(ValueError, match="current reference permission"):
            await reference.register(session, action_id=1, business_key="B1")
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0
        assert await session.scalar(select(func.count()).select_from(ReliableMessage)) == 0


async def test_action_freezes_authoritative_version_and_replay_ignores_new_permission(reference):
    reference.facts["B1"] = {"can_register": True, "device_code": "D1", "version": 7}
    async with reference.ecs.factory.begin() as session:
        first = await reference.register(session, action_id=1)
    reference.facts["B1"] = {"can_register": False, "device_code": "D2", "version": 9}
    async with reference.ecs.factory.begin() as session:
        reads = len(reference.fact_reads)
        again = await reference.register(session, action_id=1)
        assert again.command_code == first.command_code
        assert len(reference.fact_reads) == reads
        row = await reference.state(session)
        assert row["intent_version"] == 7 and row["intent_device_code"] == "D1"
        with pytest.raises(ValueError, match="different input"):
            await reference.register(session, action_id=1, params={"qty": 2})
        with pytest.raises(ValueError, match="different input"):
            await reference.register(session, action_id=1, business_key="B2")


async def test_independent_authorized_business_is_not_blocked_by_device_history(reference):
    async with reference.ecs.factory.begin() as session:
        first = await reference.register(session, action_id=1)
    await reference.apply_result(first.command_code, "FAILED")
    async with reference.ecs.factory.begin() as session:
        assert await reference.consume(session, action_id=1)
    reference.facts["B1"] = {"can_register": False, "device_code": "D1", "version": 1}
    reference.facts["B2"] = {"can_register": True, "device_code": "D1", "version": 8}
    async with reference.ecs.factory.begin() as session:
        second = await reference.register(session, action_id=2, business_key="B2")
        assert second.command_code != first.command_code
        row = await reference.state(session, action_id=2)
        assert row["intent_version"] == 8 and row["device_code"] == "D1"
        assert (await reference.state(session))["state"] == "FAILED"


async def test_cancelled_draft_does_not_create_a_command(reference):
    from app.models import EcsCommand

    async with reference.ecs.factory.begin() as session:
        assert await reference.change(session, action_id=1, kind="cancel")
    async with reference.ecs.factory.begin() as session:
        with pytest.raises(ValueError, match="current reference permission"):
            await reference.register(session, action_id=1)
        assert (await reference.state(session))["state"] == "CANCELLED"
        assert await session.scalar(select(func.count()).select_from(EcsCommand)) == 0


@pytest.mark.parametrize("seconds", [None, "300", True, 0, -1, 60, 600, 299.999, 300.001, 300])
async def test_new_commands_require_protocol_retry_window(ecs, monkeypatch, seconds):
    from dataclasses import replace
    from datetime import timedelta

    from app.core.errors import AppError, ErrorCode
    from app.models import EcsCommand

    resolve = ecs.routes.resolve_declared

    async def route(*args):
        window = timedelta(seconds=seconds) if type(seconds) in (int, float) else seconds
        return replace(await resolve(*args), retry_window=window)

    monkeypatch.setattr(ecs.routes, "resolve_declared", route)
    async with ecs.factory.begin() as session:
        if seconds != 300:
            with pytest.raises(AppError) as error:
                await create(ecs, session)
            assert error.value.code == ErrorCode.DATABASE_UNAVAILABLE
            assert "PROTOCOL_RETRY_WINDOW" in error.value.message
            for table in (EcsCommand, ReliableMessage):
                assert await session.scalar(select(func.count()).select_from(table)) == 0
        else:
            await create(ecs, session)
            command = await session.scalar(select(EcsCommand))
            message = await session.scalar(select(ReliableMessage))
            assert command.route["retry_window"] == 300
            assert command.expires_at == message.expires_at
            assert command.expires_at - command.created_at == timedelta(seconds=300)


@pytest.mark.parametrize("seconds", [60, 600])
@pytest.mark.parametrize("policy", ["invalid", "unavailable"])
async def test_existing_calls_preserve_frozen_retry_window(ecs, monkeypatch, seconds, policy):
    from datetime import timedelta

    from app.models import EcsCommand

    async with ecs.factory.begin() as session:
        original = await create(ecs, session)
        row = await session.get(EcsCommand, original.command_code)
        # Persisted pre-upgrade fixture, not a new command through an obsolete policy.
        row.route = {**row.route, "retry_window": seconds}
        row.expires_at = row.created_at + timedelta(seconds=seconds)
        message = await session.scalar(select(ReliableMessage))
        message.expires_at = row.expires_at
        frozen = (dict(row.route), row.expires_at, row.request_raw)

    async def unavailable(*args):
        raise AssertionError("old call must not resolve current routing")

    if policy == "invalid":
        from dataclasses import replace

        resolve = ecs.routes.resolve_declared

        async def invalid(*args):
            return replace(await resolve(*args), retry_window=timedelta(seconds=12))

        monkeypatch.setattr(ecs.routes, "resolve_declared", invalid)
    else:
        monkeypatch.setattr(ecs.routes, "resolve_declared", unavailable)
    calls = ecs.routes.calls
    async with ecs.factory.begin() as session:
        assert (await create(ecs, session)).command_code == original.command_code
        with pytest.raises(ValueError, match="different input"):
            await create(ecs, session, qty=2)
        row = await session.get(EcsCommand, original.command_code)
        assert (row.route, row.expires_at, row.request_raw) == frozen
        assert (await session.scalar(select(ReliableMessage))).expires_at == frozen[1]
        assert ecs.routes.calls == calls
