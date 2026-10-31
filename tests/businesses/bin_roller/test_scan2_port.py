"""The production seam owns its decision; roller owns original arrival and release."""

import pytest
from sqlalchemy import select


async def test_scan2_fact_precedes_business_admission_and_closes_only_component_work(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        admitted = (await session.execute(select(flow.production_table))).mappings().one()
        assert admitted["arrival_id"] == arrival["id"]
        assert admitted["raw_bin_code"] == "A000000001-C"
        assert admitted["scanned_at"] == arrival["scanned_at"] == 10
        assert arrival["delegated"] and not arrival["pending"]
        assert not await flow.messages(session)


@pytest.mark.parametrize("owner", ["first", "second"])
async def test_two_productions_share_one_scan2_entry(roller_scans, owner):
    flow = roller_scans
    flow.rebuild(owner)
    flow.production.release_requested = True
    await flow.scan("SCAN2", "A000000001-C")
    async with flow.factory() as session:
        admitted = (await session.execute(select(flow.production_table))).mappings().one()
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        assert admitted["business"] == arrival["owner_domain"] == owner
        assert len(await flow.messages(session)) == 1


async def test_production_wait_and_recovery_do_not_readmit_or_refresh_scan_time(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    flow.rebuild()
    flow.production.release_requested = True
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory() as session:
        admitted = (await session.execute(select(flow.production_table))).mappings().all()
        assert len(admitted) == 1 and admitted[0]["scanned_at"] == 10
        assert admitted[0]["decision"] == "WAIT"
        assert not await flow.messages(session)
        assert len(await flow.rows(session, "scan_fact")) == 2


async def test_intervening_scan_starts_new_arrival_while_old_production_pending(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    await flow.scan("SCAN2", "A000000002-C", timestamp=20)
    await flow.scan("SCAN2", "A000000001-C", timestamp=30)
    async with flow.factory() as session:
        admitted = (await session.execute(select(flow.production_table))).mappings().all()
        assert len(admitted) == 3
        assert {r["scanned_at"] for r in admitted} == {10, 20, 30}
        assert not await flow.messages(session)


@pytest.mark.parametrize("raw,decision", [("NOREAD", "REJECTED"), ("A000000001-C", "NO_CONTEXT")])
async def test_business_rejection_keeps_original_scan_without_command(roller_scans, raw, decision):
    flow = roller_scans
    flow.production.current_valid = False
    flow.production.decision = decision
    await flow.scan("SCAN2", raw)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_fact")) == 1
        assert (
            await session.execute(select(flow.production_table.c.decision))
        ).scalar_one() == decision
        assert not await flow.messages(session)


async def test_admission_and_release_intent_roll_back_with_outer_transaction(roller_scans):
    flow = roller_scans
    flow.production.release_requested = True
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with flow.factory.begin() as session:
            await flow.scan("SCAN2", "A000000001-C", timestamp=10, session=session)
            assert len(await flow.messages(session)) == 1
            raise RuntimeError("outer rollback")
    async with flow.factory() as session:
        assert (await session.execute(select(flow.production_table))).all() == []
        assert not await flow.rows(session, "scan_arrival")
        assert not await flow.messages(session)
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan2_release")) == 1
        assert len(await flow.messages(session)) == 1


async def test_production_storage_failure_rolls_back_then_original_message_can_replay(roller_scans):
    flow = roller_scans
    flow.production.fail = True
    with pytest.raises(RuntimeError, match="production storage failure"):
        await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    flow.production.fail = False
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_fact")) == 1
        assert len((await session.execute(select(flow.production_table))).all()) == 1
