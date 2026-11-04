"""Host admission, explicit release and original command consumption are separate."""

import pytest

from app.businesses._components.bin_roller.ports import RollerWorkRef


@pytest.mark.parametrize(
    "decision", ["NO_WORK", "WORK_REQUIRED", "WAIT", "NORMAL_COMPLETION", "NG_COMPLETION"]
)
async def test_release_requires_explicit_original_business_call(roller_scans, decision):
    flow = roller_scans
    flow.production.decision = decision
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory.begin() as session:
        row = (await flow.rows(session, "bin_passage"))[0]
        assert len(await flow.messages(session)) == 0
        assert row["production_record_id"]
        code = await flow.roller.release_scan2(session, arrival_id=row["id"])
        assert code == await flow.roller.release_scan2(session, arrival_id=row["id"])
        assert len(await flow.messages(session)) == 1
        result = await flow.roller.get_scan2_release_result(session, arrival_id=row["id"])
        assert result.command_code == code and result.result is None


async def test_old_completion_and_existing_release_result_cannot_release_new_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    async with flow.factory.begin() as session:
        first = (await flow.rows(session, "bin_passage"))[0]
        code = await flow.roller.release_scan2(session, arrival_id=first["id"])
    await flow.result(code, "SCAN2")
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    flow.rebuild()
    async with flow.factory.begin() as session:
        rows = await flow.rows(session, "bin_passage")
        assert len(rows) == 2 and rows[1]["release_command_code"] is None
        assert await flow.roller.release_scan2(session, arrival_id=first["id"]) == code
        assert len(await flow.messages(session)) == 1


async def test_release_rejects_other_business_and_non_scan2_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B")
    async with flow.factory.begin() as session:
        row = (await flow.rows(session, "bin_passage"))[0]
        with pytest.raises(ValueError):
            await flow.roller.release_scan2(session, arrival_id=row["id"])
    await flow.scan("SCAN2", "A000000002-C")
    async with flow.factory() as session:
        row = (await flow.rows(session, "bin_passage"))[-1]
    flow.rebuild("second")
    async with flow.factory.begin() as session:
        with pytest.raises(ValueError):
            await flow.roller.release_scan2(session, arrival_id=row["id"])


async def test_scan3_and_scan4_do_not_wait_for_upstream_result(roller_current):
    flow = roller_current
    await flow.scan("SCAN2", "A000000001-C")
    await flow.current_bins(1)
    row = (await flow.current_rows("bin_return"))[0]
    assert row["scan4_consumed_result_id"] and row["scan3_consumed_result_id"] is None
    assert row["return_state"] == "READY"


async def test_unassociated_scan4_keeps_fact_without_forcing_latest_round(roller_current):
    flow = roller_current
    await flow.scan("SCAN3", "A000000001-B")
    await flow.scan("SCAN4", "A000000002-B")
    rows = await flow.current_rows("bin_return")
    orphan = next(r for r in rows if r["bin_code"] == "A000000002")
    assert orphan["scan3_event_id"] is None and orphan["scan4_event_id"]
    assert orphan["scan4_command_code"] is None and orphan["return_state"] == "OBSERVED"


async def test_release_work_consumes_original_result_without_creating_a_command(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN2", "A000000001-C")
    async with flow.factory.begin() as session:
        row = (await flow.rows(session, "bin_passage"))[0]
        code = await flow.roller.release_scan2(session, arrival_id=row["id"])
        work = RollerWorkRef("SCAN2_RELEASE", row["id"])
        assert await flow.roller.advance(session, work) == "WAITING"
    await flow.result(code, "SCAN2")
    flow.rebuild()
    async with flow.factory.begin() as session:
        assert await flow.roller.advance(session, work) == "SUCCESS"
        await flow.roller.advance(session, work)
        consumed = await flow.roller.original_record(session, "bin_passage", row["id"])
        assert consumed["release_consumed_result_id"]
        assert len(await flow.messages(session)) == 1


@pytest.mark.parametrize("status", [None, "SUCCESS"])
async def test_scan4_new_content_never_reexecutes_unresolved_or_successful_round(
    roller_current, status
):
    flow = roller_current
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=20)
    first = (await flow.current_rows("bin_return"))[0]
    if status:
        await flow.result(first["scan4_command_code"], "SCAN4")
    await flow.scan("SCAN4", "NOREAD", timestamp=30)
    await flow.scan("SCAN4", "A000000001-B", timestamp=40)
    row = next(r for r in await flow.current_rows("bin_return") if r["id"] == first["id"])
    assert (
        row["scan4_command_code"] == first["scan4_command_code"]
        and row["fifo_visit_key"] == first["fifo_visit_key"]
    )


async def test_earlier_unassociated_scan4_does_not_bind_to_later_scan3_round(roller_current):
    flow = roller_current
    await flow.scan("SCAN4", "A000000001-B", timestamp=10)
    orphan = (await flow.current_rows("bin_return"))[0]
    await flow.scan("SCAN3", "A000000001-B", timestamp=20)
    await flow.scan("SCAN4", "A000000001-B", timestamp=30)
    unchanged = next(r for r in await flow.current_rows("bin_return") if r["id"] == orphan["id"])
    assert unchanged["scan3_event_id"] is None and unchanged["scan4_command_code"] is None
    async with flow.factory() as session:
        assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
