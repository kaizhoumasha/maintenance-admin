import pytest

from app.integrations.ecs_callback import get_event


async def test_scan2_uses_unique_original_forward_passage_without_waiting_for_result(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory() as session:
        rows = await flow.rows(session, "bin_passage")
        assert len(rows) == 1
        row = rows[0]
        assert row["scan1_event_id"] and row["scan2_event_id"]
        assert row["scan1_consumed_result_id"] is None
        assert row["production_record_id"] == row["id"]
        command = await flow.ecs.get_command(
            session, owner=flow.roller.context, command_code=row["scan1_command_code"]
        )
        assert command.task_type == "MOVE_FORWARD" and command.device_code == flow.devices["SCAN1"]
        event = await get_event(session, event_id=row["scan2_event_id"])
        assert event.record.application_ref.subject_id == row["id"]


@pytest.mark.parametrize(
    "scenario",
    ["no_predecessor", "multiple", "sideways", "unread", "different_task", "closed", "scan2_first"],
)
async def test_ineligible_or_ambiguous_scan2_remains_independent(roller_scans, scenario):

    flow = roller_scans
    if scenario == "scan2_first":
        await flow.scan("SCAN2", "A000000001-C", timestamp=1)
    if scenario == "sideways":
        flow.production.current_valid = False
    if scenario != "no_predecessor":
        await flow.scan("SCAN1", "NOREAD" if scenario == "unread" else "A000000001-B", timestamp=10)
    flow.production.current_valid = True
    if scenario == "multiple":
        await flow.scan("SCAN1", "A000000002-B", timestamp=11)
        await flow.scan("SCAN1", "A000000001-B", timestamp=12)
    if scenario in ("different_task", "closed"):
        async with flow.factory.begin() as session:
            rows = await flow.rows(session, "bin_passage")
            repo = flow.roller.repository(1)
            if scenario == "different_task":
                await repo.update(session, "bin_passage", rows[0]["id"], scope_id="old-task")
            else:
                await repo.close(session, "bin_passage", rows[0]["id"], "TASK_COMPLETED")
    if scenario != "scan2_first":
        await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory() as session:
        rows = await flow.rows(session, "bin_passage")
        assert all(not (r["scan1_event_id"] and r["scan2_event_id"]) for r in rows)
        assert len([r for r in rows if r["production_record_id"]]) == 1


async def test_equivalent_scan2_reuses_point_before_searching_for_new_predecessor(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    await flow.scan("SCAN2", "A000000001-C", timestamp=21)
    async with flow.factory() as session:
        rows = await flow.rows(session, "bin_passage")
        assert len(rows) == 1 and rows[0]["production_record_id"]


async def test_true_return_round_and_failed_scan4_rescan_keep_first_fifo_position(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN3", "A000000001-B", timestamp=10)
    await flow.scan("SCAN4", "A000000001-B", timestamp=11)
    async with flow.factory() as session:
        (original,) = await flow.rows(session, "bin_return")
    await flow.result(original["scan4_command_code"], "SCAN4", status="FAILED")
    await flow.scan("SCAN4", "A000000001-B", timestamp=12)
    async with flow.factory() as session:
        (current,) = await flow.rows(session, "bin_return")
        assert current["id"] == original["id"]
        assert current["first_scan4_event_id"] == original["first_scan4_event_id"]
        assert current["fifo_visit_key"] == original["fifo_visit_key"]
        assert current["scan4_command_code"] != original["scan4_command_code"]
    await flow.result(current["scan4_command_code"], "SCAN4")
    from app.businesses._components.bin_roller.ports import RollerWorkRef

    async with flow.factory.begin() as session:
        await flow.roller.advance(session, RollerWorkRef("RETURN", current["id"]))
        assert [
            r["id"] for r in await flow.roller.get_return_candidates(session, work_line_id=1)
        ] == [current["id"]]
