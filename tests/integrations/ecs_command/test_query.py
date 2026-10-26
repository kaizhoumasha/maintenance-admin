import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


async def test_example_consumer_advances_once_in_its_own_transaction(reference):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    assert (await reference.apply_result(command.command_code)).status_code == 200

    async def consume():
        async with ecs.factory.begin() as session:
            return await reference.consume(session, action_id=1)

    if ecs.engine.dialect.name == "postgresql":
        winners = await asyncio.gather(consume(), consume())
    else:
        winners = [await consume(), await consume()]
    assert sum(winners) == 1
    async with ecs.factory() as session:
        assert (await reference.state(session))["state"] == "SUCCEEDED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(reference.actions)
                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
            )
            == 1
        )


@pytest.mark.parametrize("result", ["SUCCESS", "FAILED"])
async def test_consumer_write_failure_rolls_back_marker_and_can_retry(
    reference, monkeypatch, result
):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    await reference.apply_result(command.command_code, result)
    with pytest.raises(IntegrityError):
        async with ecs.factory.begin() as session:
            execute = session.execute

            async def fail_business_write(statement, *args, **kwargs):
                if statement.is_update and statement.table is reference.actions:
                    statement = statement.values(state="INVALID")
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", fail_business_write)
            await reference.consume(session, action_id=1)
    async with ecs.factory.begin() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(reference.actions)
                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
            )
            == 0
        )
        assert (await reference.state(session))["state"] == "WAITING_RESULT"
        assert await reference.consume(session, action_id=1)
    async with ecs.factory() as session:
        assert (await reference.state(session))["state"] == (
            "SUCCEEDED" if result == "SUCCESS" else "FAILED"
        )
        assert (
            await ecs.service.get_command(
                session,
                owner=ecs.owner,
                command_code=command.command_code,
            )
        ).result["result"] == result


@pytest.mark.parametrize("case", ["owner", "cancel", "rebind", "version", "conflict"])
async def test_result_does_not_override_owner_mapping_permission_or_conflict(reference, case):
    from app.integrations.ecs_command import CommandOwner

    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    await reference.apply_result(command.command_code)
    if case == "conflict":
        assert (await reference.apply_result(command.command_code, "FAILED")).status_code == 409
    async with ecs.factory.begin() as session:
        if case in ("cancel", "rebind", "version"):
            await reference.change(session, action_id=1, kind=case)
        before = dict(await reference.state(session))
        reference.owner = CommandOwner("other") if case == "owner" else ecs.owner
        assert not await reference.consume(session, action_id=1)
        assert dict(await reference.state(session)) == before
        assert (
            await session.scalar(
                select(func.count())
                .select_from(reference.actions)
                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
            )
            == 0
        )
    async with ecs.factory() as session:
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.result["result"] == "SUCCESS"


async def test_late_result_conflict_does_not_undo_consumed_business(reference):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    await reference.apply_result(command.command_code)
    async with ecs.factory.begin() as session:
        assert await reference.consume(session, action_id=1)
    assert (await reference.apply_result(command.command_code, "FAILED")).status_code == 409
    async with ecs.factory.begin() as session:
        assert not await reference.consume(session, action_id=1)
        assert (await reference.state(session))["state"] == "SUCCEEDED"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(reference.actions)
                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
            )
            == 1
        )


@pytest.mark.parametrize("change", ["cancel", "rebind", "version"])
@pytest.mark.parametrize("first", ["change", "consume"])
async def test_consumption_and_business_change_follow_lock_order(
    reference, wait_for_lock, change, first
):
    from sqlalchemy import text

    ecs = reference.ecs
    if ecs.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL reference business row locking")
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    await reference.apply_result(command.command_code)
    pid_ready = asyncio.Future()

    async def second():
        async with ecs.factory.begin() as session:
            pid_ready.set_result(await session.scalar(text("SELECT pg_backend_pid()")))
            if first == "change":
                return await reference.consume(session, action_id=1)
            return await reference.change(session, action_id=1, kind=change)

    async with ecs.factory.begin() as blocker:
        if first == "change":
            assert await reference.change(blocker, action_id=1, kind=change)
        else:
            assert await reference.consume(blocker, action_id=1)
        pending = asyncio.create_task(second())
        try:
            await wait_for_lock(pending, await asyncio.wait_for(pid_ready, 5))
            assert not pending.done()
        finally:
            await blocker.commit()
            second_result = await asyncio.wait_for(pending, 5)
    assert not second_result
    async with ecs.factory() as session:
        state = await reference.state(session)
        count = await session.scalar(
            select(func.count())
            .select_from(reference.actions)
            .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
        )
        assert count == (1 if first == "consume" else 0)
        if first == "consume":
            assert (
                state["state"] == "SUCCEEDED"
                and state["device_code"] == "D1"
                and state["version"] == 1
            )
        else:
            assert state["state"] == ("CANCELLED" if change == "cancel" else "WAITING_RESULT")
            assert state["version"] == 2
            assert state["device_code"] == ("D2" if change == "rebind" else "D1")
        view = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert view.result["result"] == "SUCCESS"


@pytest.mark.parametrize(
    "fact",
    [
        None,
        {"can_register": False, "device_code": "D1", "version": 1},
        {"can_register": True, "device_code": "D2", "version": 1},
        {"can_register": True, "device_code": "D1", "version": 2},
    ],
    ids=["missing", "revoked", "rebound", "new-version"],
)
async def test_consumer_requires_current_authority_even_when_action_is_unchanged(reference, fact):
    ecs = reference.ecs
    async with ecs.factory.begin() as session:
        command = await reference.register(session, action_id=1)
    assert (await reference.apply_result(command.command_code)).status_code == 200
    async with ecs.factory() as session:
        before = dict(await reference.state(session))
    reference.facts["B1"] = fact
    async with ecs.factory.begin() as session:
        assert not await reference.consume(session, action_id=1)
        assert dict(await reference.state(session)) == before
        assert before["state"] == "WAITING_RESULT"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(reference.actions)
                .where(reference.actions.c.state.in_(("SUCCEEDED", "FAILED")))
            )
            == 0
        )
        result = await ecs.service.get_command(
            session, owner=ecs.owner, command_code=command.command_code
        )
        assert result.result["result"] == "SUCCESS"
