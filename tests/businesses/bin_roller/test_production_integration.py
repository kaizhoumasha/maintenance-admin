"""Test-owned production policies cross the actual component receiver/assembly boundary."""

import pytest
from sqlalchemy import select, text

from app.models import EcsCommand


async def test_two_businesses_have_separate_production_ports_and_original_owners(
    roller_application,
):
    flow = roller_application
    first, second = (flow.hosts[owner] for owner in ("first", "second"))
    assert first.production is not second.production
    assert first.components["bin_roller"] is not second.components["bin_roller"]
    assert first.capabilities.wms.owner_domain == "first"
    assert second.capabilities.wms.owner_domain == "second"
    first.production.permit = True
    for timestamp, owner in enumerate(("first", "second"), 1):
        response = await flow.application_scan(owner, "SCAN2", "A000000001-B", timestamp=timestamp)
        assert response.status_code == 200
    async with flow.factory() as session:
        rows = (await session.execute(select(flow.production_table))).mappings().all()
        commands = (await session.scalars(select(EcsCommand))).all()
        assert {(row["business"], row["release_requested"]) for row in rows} == {
            ("first", True),
            ("second", False),
        }
        assert [command.owner_domain for command in commands] == ["first"]
    second_id = next(row["arrival_id"] for row in rows if row["business"] == "second")
    with pytest.raises(ValueError, match="original roller record is missing"):
        async with flow.factory.begin() as session:
            await first.components["bin_roller"].release_scan2(session, arrival_id=second_id)
    await second.complete_production(second_id)
    async with flow.factory() as session:
        commands = (await session.scalars(select(EcsCommand))).all()
        assert {command.owner_domain for command in commands} == {"first", "second"}
    assert flow.notifications == ["first", "second"]


@pytest.mark.parametrize("permit", [False, True])
async def test_scan2_replay_and_pending_do_not_repeat_admission_or_release(
    roller_application, permit
):
    flow = roller_application
    host = flow.hosts["first"]
    host.production.permit = permit
    await flow.application_scan("first", "SCAN2", "A000000001-B")
    await flow.application_scan("first", "SCAN2", "A000000001-B")
    await host.scan_pending()
    async with flow.factory() as session:
        production = (await session.execute(select(flow.production_table))).mappings().one()
        commands = (await session.scalars(select(EcsCommand))).all()
        assert len(commands) == int(permit)
        assert production["release_requested"] is permit
    host.production.valid = False
    await host.complete_production(production["arrival_id"])
    await host.complete_production(production["arrival_id"])
    async with flow.factory() as session:
        command = (await session.scalars(select(EcsCommand))).one()
        original_code = command.command_code
    await flow.result(original_code, "SCAN2")
    flow.application_rebuild()
    await flow.hosts["first"].scan_pending()
    await flow.hosts["first"].scan_pending()
    async with flow.factory() as session:
        commands = (await session.scalars(select(EcsCommand))).all()
        assert [command.command_code for command in commands] == [original_code]
        assert len((await session.execute(select(flow.production_table))).all()) == 1
        release = (await flow.rows(session, "scan2_release"))[0]
        assert release["pending"] is False
    assert flow.notifications == ["first"]


async def test_outer_rollback_after_savepoint_discards_intent_and_notification(roller_application):
    flow = roller_application
    flow.hosts["first"].production.permit = True
    async with flow.factory() as session:
        await session.begin()
        if session.get_bind().dialect.name == "sqlite":
            await session.execute(text("BEGIN"))
        async with session.begin_nested():
            await flow.application_scan("first", "SCAN2", "A000000001-B", session=session)
        assert not flow.notifications
        await session.rollback()
    async with flow.factory() as session:
        assert not await flow.rows(session, "scan_fact")
        assert not (await session.execute(select(flow.production_table))).all()
        assert not await flow.messages(session)
    assert not flow.notifications
    response = await flow.application_scan("first", "SCAN2", "A000000001-B")
    assert response.outcome == "received"
    assert flow.notifications == ["first"]


async def test_committed_work_recovers_after_lost_notification(roller_application):
    flow = roller_application

    def unavailable(_):
        raise ConnectionError("test publisher unavailable")

    response = await flow.application_scan("first", "SCAN1", "A000000001-B", notify=unavailable)
    assert response.status_code == 200
    async with flow.factory() as session:
        scan = (await flow.rows(session, "scan_arrival"))[0]
        command = await flow.command(session, scan["command_code"])
        original = (command.command_code, command.request_raw)
    await flow.result(original[0], "SCAN1")
    flow.application_rebuild()
    await flow.runtime.progressions["first"]()
    async with flow.factory() as session:
        scan = (await flow.rows(session, "scan_arrival"))[0]
        command = await flow.command(session, scan["command_code"])
        assert (command.command_code, command.request_raw) == original
        assert scan["pending"] is False
