"""Explicit production permission and original ECS Result remain separate facts."""

import pytest


@pytest.mark.parametrize(
    "production,decision,immediate",
    [
        ("manual_pick", "NO_WORK", True),
        ("manual_putaway", "NO_WORK", True),
        ("manual_pick", "WORK_REQUIRED", False),
        ("manual_putaway", "WAIT", False),
        ("manual_pick", "NORMAL_COMPLETION", False),
        ("manual_putaway", "NG_COMPLETION", False),
        ("manual_pick", "EARLY_COMPLETION_AWAITS_REVIEW", False),
        ("automatic_pick", "NO_WORK", True),
        ("automatic_pick", "REPORT_ACCEPTED_WITH_SUCCESSOR", False),
        ("automatic_putaway", "NO_WORK", True),
        ("automatic_putaway", "FINAL_ACCEPT_WITH_REPORT_PENDING", False),
    ],
)
async def test_release_requires_explicit_original_business_call(
    roller_scans, production, decision, immediate
):
    flow = roller_scans
    flow.production.decision = production + ":" + decision
    flow.production.release_requested = immediate
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory.begin() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        assert len(await flow.messages(session)) == int(immediate)
        # The test supplies the production's eventual permission; it does not implement its policy.
        first = await flow.roller.release_scan2(session, arrival_id=arrival["id"])
        again = await flow.roller.release_scan2(session, arrival_id=arrival["id"])
        assert first == again
        assert len(await flow.messages(session)) == 1
        result = await flow.roller.get_scan2_release_result(session, arrival_id=arrival["id"])
        assert result.command_code == first and result.result is None


async def test_old_completion_and_existing_release_result_cannot_release_new_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory.begin() as session:
        first = (await flow.rows(session, "scan_arrival"))[0]
        original = await flow.roller.release_scan2(session, arrival_id=first["id"])
    await flow.result(original, "SCAN2")
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    flow.rebuild()
    async with flow.factory.begin() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        assert len(arrivals) == 2
        assert await flow.roller.release_scan2(session, arrival_id=first["id"]) == original
        result = await flow.roller.get_scan2_release_result(session, arrival_id=first["id"])
        assert result.result["result"] == "SUCCESS"
        assert (
            await flow.roller.get_scan2_release_result(session, arrival_id=arrivals[1]["id"])
            is None
        )
        assert len(await flow.messages(session)) == 1


async def test_release_rejects_other_business_and_non_scan2_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B")
    async with flow.factory.begin() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        with pytest.raises(ValueError):
            await flow.roller.release_scan2(session, arrival_id=arrival["id"])
    await flow.scan("SCAN2", "A000000001-C")
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[-1]
    flow.rebuild("second")
    async with flow.factory.begin() as session:
        with pytest.raises(ValueError):
            await flow.roller.release_scan2(session, arrival_id=arrival["id"])


async def test_scan3_and_scan4_do_not_wait_for_upstream_result(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C")
    await flow.scan("SCAN3", "A000000001-B")
    await flow.scan("SCAN4", "A000000001-B")
    async with flow.factory() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        scan3, scan4 = arrivals[-2:]
        assert scan3["return_round_id"] == scan4["return_round_id"] is not None
        assert scan3["command_code"] and scan4["command_code"]
        assert len(await flow.messages(session)) == 2
        result = await flow.roller.get_scan_result(session, arrival_id=scan4["id"])
        assert result.result is None
    await flow.result(scan4["command_code"], "SCAN4")
    async with flow.factory() as session:
        result = await flow.roller.get_scan_result(session, arrival_id=scan4["id"])
        assert result.result["result"] == "SUCCESS"
        upstream = await flow.roller.get_scan_result(session, arrival_id=scan3["id"])
        assert upstream.result is None


async def test_unassociated_scan4_keeps_fact_without_forcing_latest_round(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B")
    await flow.scan("SCAN4", "A000000002-B")
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[-1]
        assert arrival["return_round_id"] is None
        assert arrival["command_code"] is None
        assert arrival["pending"]
        assert len(await flow.messages(session)) == 1


async def test_release_work_consumes_original_result_without_creating_a_command(roller_scans):
    from app.businesses._components.bin_roller.ports import RollerWorkRef

    flow = roller_scans
    flow.production.release_requested = True
    await flow.scan("SCAN2", "A000000001-C")
    async with flow.factory.begin() as session:
        release = (await flow.rows(session, "scan2_release"))[0]
        work = RollerWorkRef("SCAN2_RELEASE", release["id"])
        assert await flow.roller.advance(session, work) == "WAITING"
    await flow.result(release["command_code"], "SCAN2")
    flow.rebuild()
    async with flow.factory.begin() as session:
        assert await flow.roller.advance(session, work) == "SUCCESS"
        assert await flow.roller.advance(session, work) == "SUCCESS"
        consumed = (await flow.rows(session, "scan2_release"))[0]
        assert consumed["consumed_result_id"] and not consumed["pending"]
        assert len(await flow.messages(session)) == 1


async def test_real_scan4_failure_rescan_keeps_original_round_and_first_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    async with flow.factory() as session:
        first = (await flow.rows(session, "scan_arrival"))[-1]
    await flow.result(first["command_code"], "SCAN4", status="FAILED")
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        retry = arrivals[-1]
        round_ = (await flow.rows(session, "return_round"))[0]
        assert round_["scan4_arrival_id"] == first["id"]
        assert retry["return_round_id"] == first["return_round_id"]
        assert retry["command_code"] != first["command_code"]
        assert len(await flow.messages(session)) == 3


@pytest.mark.parametrize("status", ["pending", "SUCCESS"])
async def test_scan4_new_content_never_reexecutes_unresolved_or_successful_round(
    roller_scans, status
):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    async with flow.factory() as session:
        first = (await flow.rows(session, "scan_arrival"))[-1]
    if status == "SUCCESS":
        await flow.result(first["command_code"], "SCAN4")
    await flow.scan("SCAN4", "NOREAD", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory() as session:
        assert len(await flow.messages(session)) == 2
        round_ = (await flow.rows(session, "return_round"))[0]
        assert round_["scan4_arrival_id"] == first["id"]


async def test_orphan_history_does_not_block_proven_current_scan4(roller_scans):
    from sqlalchemy import select

    from app.core.identifiers import uuid7

    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    async with flow.factory.begin() as session:
        table = flow.store.tables["return_round"]
        original = (await session.execute(select(table))).mappings().one()
        orphan = dict(original) | {"id": str(uuid7()), "scan3_arrival_id": str(uuid7())}
        await session.execute(table.insert().values(**orphan))
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[-1]
        assert arrival["return_round_id"] == original["id"]
        assert arrival["command_code"] is not None
        assert len(await flow.messages(session)) == 2
        assert (
            dict(await flow.roller.original_record(session, "return_round", orphan["id"])) == orphan
        )


@pytest.mark.parametrize("relation", ["missing", "conflicting"])
async def test_latest_scan3_missing_relation_does_not_fall_back_to_old_round(
    roller_scans, relation
):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN3", "A000000002-B", timestamp=20)
    await flow.scan("SCAN3", "A000000001-B", timestamp=30)
    async with flow.factory.begin() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        original, current = arrivals[0], arrivals[-1]
        table = flow.store.tables["scan_arrival"]
        await session.execute(
            table.update()
            .where(table.c.id == current["id"])
            .values(return_round_id=None if relation == "missing" else original["return_round_id"])
        )
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory.begin() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[-1]
        assert arrival["return_round_id"] is None and arrival["command_code"] is None
        assert len(await flow.messages(session)) == 3
        # Repairing the test's damaged association cannot retroactively attach the saved SCAN4.
        await session.execute(
            table.update()
            .where(table.c.id == current["id"])
            .values(return_round_id=current["return_round_id"])
        )
    await flow.scan("SCAN4", "A000000001-B", timestamp=50)
    async with flow.factory() as session:
        saved = await flow.roller.original_arrival(session, arrival["id"])
        assert saved["return_round_id"] is None and saved["command_code"] is None
        assert saved["scanned_at"] == 40
        facts = [f for f in await flow.rows(session, "scan_fact") if f["point"] == "SCAN4"]
        assert {f["arrival_id"] for f in facts} == {arrival["id"]}
        assert len(await flow.messages(session)) == 3


async def test_earlier_unassociated_scan4_does_not_bind_to_later_scan3_round(roller_scans):
    from app.businesses._components.bin_roller.ports import RollerWorkRef

    flow = roller_scans
    await flow.scan("SCAN4", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        original = (await flow.rows(session, "scan_arrival"))[0]
    await flow.scan("SCAN3", "A000000001-B", timestamp=20)
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    async with flow.factory.begin() as session:
        assert (
            await flow.roller.advance(session, RollerWorkRef("SCAN", original["id"])) == "WAITING"
        )
        saved = await flow.roller.original_arrival(session, original["id"])
        assert saved["return_round_id"] is None and saved["command_code"] is None
        assert saved["scanned_at"] == 10
        facts = [f for f in await flow.rows(session, "scan_fact") if f["point"] == "SCAN4"]
        assert len(facts) == 2
        assert {f["arrival_id"] for f in facts} == {original["id"]}
        assert len(await flow.messages(session)) == 1
    # A genuine new same-point scan after different content has its own acceptance context.
    await flow.scan("SCAN4", "A000000002-B", timestamp=40)
    await flow.scan("SCAN4", "A000000001-B", timestamp=50)
    async with flow.factory() as session:
        new = (await flow.rows(session, "scan_arrival"))[-1]
        old = await flow.roller.original_arrival(session, original["id"])
        assert new["id"] != old["id"]
        assert new["return_round_id"] is not None and new["command_code"] is not None
        assert new["scanned_at"] == 50
        assert old["return_round_id"] is None and old["command_code"] is None
        assert old["scanned_at"] == 10
        assert len(await flow.messages(session)) == 2


async def test_failed_point_rescan_after_intervening_content_uses_original_round(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    async with flow.factory() as session:
        original = (await flow.rows(session, "scan_arrival"))[-1]
    await flow.result(original["command_code"], "SCAN4", status="FAILED")
    await flow.scan("SCAN4", "NOREAD", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    async with flow.factory() as session:
        retry = (await flow.rows(session, "scan_arrival"))[-1]
        assert retry["return_round_id"] == original["return_round_id"]
        assert retry["command_code"] is not None
        assert retry["command_code"] != original["command_code"]
        assert len(await flow.messages(session)) == 3


async def test_held_early_result_neither_splits_arrival_nor_recreates_release(roller_scans):
    import json

    from app.businesses._components.bin_roller.ports import RollerWorkRef
    from app.integrations.ecs_command.result import receive_ecs_result

    flow = roller_scans
    flow.production.release_requested = True
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory.begin() as session:
        release = (await flow.rows(session, "scan2_release"))[0]
        response = await receive_ecs_result(
            session,
            json.dumps(
                dict(
                    command_code=release["command_code"],
                    device_code=flow.devices["SCAN2"],
                    result="SUCCESS",
                    finish_time=100,
                    data={},
                )
            ).encode(),
            flow.ecs,
        )
        assert response.status_code == 409
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory.begin() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        assert len(arrivals) == 1
        assert (
            await flow.roller.release_scan2(session, arrival_id=arrivals[0]["id"])
            == release["command_code"]
        )
        assert (
            await flow.roller.advance(session, RollerWorkRef("SCAN2_RELEASE", release["id"]))
            == "WAITING"
        )
        assert len(await flow.messages(session)) == 1


async def test_own_scan_result_consumption_keeps_scan4_return_work_pending(roller_scans):
    from app.businesses._components.bin_roller.ports import RollerWorkRef

    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    async with flow.factory() as session:
        scan3, scan4 = await flow.rows(session, "scan_arrival")
    await flow.result(scan4["command_code"], "SCAN4")
    async with flow.factory.begin() as session:
        assert await flow.roller.advance(session, RollerWorkRef("SCAN", scan4["id"])) == "SUCCESS"
        assert await flow.roller.advance(session, RollerWorkRef("SCAN", scan3["id"])) == "WAITING"
        saved = await flow.roller.original_arrival(session, scan4["id"])
        assert saved["consumed_result_id"] and saved["pending"]
        assert len(await flow.messages(session)) == 2
