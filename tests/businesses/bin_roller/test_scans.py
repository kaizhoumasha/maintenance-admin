"""Original receiver facts and strict rolling directions, without upstream gates."""

import json

import pytest

from tests.helpers import read_history_body


def test_roller_declares_one_scan_contract_and_exact_empty_directions():
    from pydantic import ValidationError

    from app.businesses._components.bin_roller.ecs_command import (
        MoveForward,
        MoveLeft,
        MoveRight,
    )
    from app.integrations.ecs_command import compile_commands

    bindings, _ = compile_commands(MoveForward, MoveLeft, MoveRight)
    assert {(b.device_role, b.task_type) for b in bindings} == {
        ("SCAN1", "MOVE_FORWARD"),
        ("SCAN2", "MOVE_FORWARD"),
        ("SCAN3", "MOVE_FORWARD"),
        ("SCAN4", "MOVE_FORWARD"),
        ("SCAN1", "MOVE_RIGHT"),
        ("SCAN3", "MOVE_LEFT"),
    }
    for command in (MoveForward, MoveLeft, MoveRight):
        assert command().model_dump() == {}
        with pytest.raises(ValidationError):
            command(bin_code="A000000001")


@pytest.mark.parametrize(
    "point,raw,valid,want",
    [
        ("SCAN1", "A000000001-B", True, "MOVE_FORWARD"),
        ("SCAN1", "A000000999-B", True, "MOVE_FORWARD"),
        ("SCAN1", "A000000001-B", False, "MOVE_RIGHT"),
        ("SCAN1", "A000000001-C", True, "MOVE_RIGHT"),
        ("SCAN1", "NOREAD", True, "MOVE_RIGHT"),
        ("SCAN3", "A000000001-B", False, "MOVE_FORWARD"),
        ("SCAN3", "A000000001-C", True, "MOVE_LEFT"),
        ("SCAN3", "NOREAD", True, "MOVE_LEFT"),
    ],
)
async def test_scan_direction_uses_only_local_suffix_and_current_task(
    roller_scans, point, raw, valid, want
):
    flow = roller_scans
    flow.production.current_valid = valid
    response = await flow.scan(point, raw)
    assert response.status_code == 200
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        command = await flow.command(session, arrival["command_code"])
        assert command.task_type == want
        assert command.owner_domain == "first"
        assert command.device_code == flow.devices[point]
        assert (
            json.loads(await read_history_body(session, (command).request_record_id))["params"]
            == {}
        )
        assert (command.expires_at - command.created_at).total_seconds() == 300
        assert len(await flow.messages(session)) == 1


async def test_consecutive_equal_scans_share_arrival_and_original_message_replay(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    await flow.scan("SCAN1", "A000000001-B", timestamp=20)
    await flow.scan("SCAN1", "A000000002-B", timestamp=30)
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        facts = await flow.rows(session, "scan_fact")
        arrivals = await flow.rows(session, "scan_arrival")
        assert len(facts) == 3 and len(arrivals) == 2
        assert facts[0]["arrival_id"] == facts[1]["arrival_id"]
        assert arrivals[0]["scanned_at"] == 10
        assert len(await flow.messages(session)) == 2
        passages = await flow.rows(session, "bin_passage")
        assert {r["bin_code"] for r in passages} == {"A000000001", "A000000002"}


async def test_distinct_raw_scans_are_not_merged_by_null_parse(roller_scans):
    await roller_scans.scan("SCAN1", "NOREAD", timestamp=10)
    await roller_scans.scan("SCAN1", "BROKEN", timestamp=20)
    async with roller_scans.factory() as session:
        rows = await roller_scans.rows(session, "scan_arrival")
        assert len(rows) == 2
        assert [r["bin_code"] for r in rows] == [None, None]


async def test_other_point_and_task_changes_do_not_split_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    flow.production.current_valid = False
    await flow.scan("SCAN2", "A000000002-C", timestamp=20)
    await flow.scan("SCAN1", "A000000001-B", timestamp=30)
    async with flow.factory() as session:
        rows = await flow.rows(session, "scan_arrival")
        assert len([r for r in rows if r["point"] == "SCAN1"]) == 1
        assert len(await flow.messages(session)) == 1


async def test_original_point_result_separates_once_before_beat_and_after_restart(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        first = (await flow.rows(session, "scan_arrival"))[0]
    await flow.result(first["command_code"], "SCAN1")
    flow.rebuild()
    await flow.scan("SCAN1", "A000000001-B", timestamp=20)
    await flow.result(first["command_code"], "SCAN1")
    await flow.scan("SCAN1", "A000000001-B", timestamp=30)
    async with flow.factory() as session:
        arrivals = await flow.rows(session, "scan_arrival")
        assert len(arrivals) == 2
        assert arrivals[1]["separator_command_code"] == first["command_code"]
        assert len(await flow.messages(session)) == 2


async def test_old_result_does_not_split_current_different_arrival(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        first = (await flow.rows(session, "scan_arrival"))[0]
    await flow.scan("SCAN1", "A000000002-B", timestamp=20)
    await flow.result(first["command_code"], "SCAN1")
    await flow.scan("SCAN1", "A000000002-B", timestamp=30)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_arrival")) == 2


async def test_storage_failure_rolls_back_receipt_facts_and_commands(roller_scans):
    flow = roller_scans
    with pytest.raises(RuntimeError, match="outer rollback"):
        async with flow.factory.begin() as session:
            await flow.scan("SCAN1", "A000000001-B", timestamp=10, session=session)
            raise RuntimeError("outer rollback")
    async with flow.factory() as session:
        assert not await flow.rows(session, "scan_fact")
        assert not await flow.messages(session)
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_fact")) == 1
        assert len(await flow.messages(session)) == 1


async def test_scan_payload_preserves_unreadable_but_rejects_wrong_json_type(roller_scans):
    flow = roller_scans
    assert (await flow.scan("SCAN1", "")).status_code == 200
    assert (await flow.scan("SCAN1", 23)).status_code == 400
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_fact")) == 1


async def test_original_message_replay_keeps_receipt_owner_after_binding_change(roller_scans):
    from sqlalchemy import select

    flow = roller_scans
    first = await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory.begin() as session:
        original = list(await flow.rows(session, "scan_fact"))
    flow.rebuild("second")
    flow.work_line_id = 2
    flow.production.release_requested = True
    await flow.scan("SCAN2", "A000000001-C", timestamp=10)
    await flow.scan("SCAN2", "A000000001-C", timestamp=20)
    async with flow.factory() as session:
        facts = await flow.rows(session, "scan_fact")
        assert list(facts) == original
        assert facts[0]["digest"] == first.digest
        assert facts[0]["binding_snapshot"]["owner_domain"] == "first"
        assert facts[0]["binding_snapshot"]["work_line_id"] == 1
        admitted = (await session.execute(select(flow.production_table))).mappings().all()
        assert len(admitted) == 1 and admitted[0]["business"] == "first"
        assert not await flow.messages(session)
    # A genuinely different message belongs to its newly resolved binding.
    await flow.scan("SCAN2", "A000000001-C", timestamp=30)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "scan_arrival")) == 2
        assert not await flow.messages(session)


@pytest.mark.parametrize("roller_store", ["postgres"], indirect=True)
async def test_concurrent_equal_scans_with_distinct_digests_share_arrival(roller_current):
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.exc import DBAPIError

    flow = roller_current
    # Seed the existing collection; both receivers establish their old snapshot before the lock.
    await flow.scan("SCAN1", "NOREAD", timestamp=1)
    barrier = asyncio.Barrier(2)
    attempts = []

    async def receive(timestamp):
        for attempt in range(3):
            try:
                async with flow.factory() as session, session.begin():
                    await session.connection(
                        execution_options={"isolation_level": "REPEATABLE READ"}
                    )
                    if attempt == 0:
                        await session.execute(select(flow.store.tables["bin_passage"]))
                        await asyncio.wait_for(barrier.wait(), 5)
                    attempts.append(attempt)
                    await flow.scan("SCAN2", "A000000001-C", timestamp=timestamp, session=session)
                return
            except DBAPIError as error:
                assert getattr(error.orig, "sqlstate", None) == "40001"
                if attempt == 2:
                    raise

    await asyncio.wait_for(asyncio.gather(receive(10), receive(20)), 15)
    async with flow.factory() as session:
        admitted = (await session.execute(select(flow.production_table))).all()
        assert len(admitted) == 1
        assert len([r for r in await flow.rows(session, "bin_passage") if r["scan2_event_id"]]) == 1
    assert 1 in attempts


async def test_result_separator_stays_with_its_original_arrival_when_stream_changes(roller_scans):
    flow = roller_scans
    await flow.scan("SCAN1", "A000000001-B", timestamp=10)
    async with flow.factory() as session:
        old = (await flow.rows(session, "bin_passage"))[0]
    await flow.scan("SCAN1", "A000000002-B", timestamp=20)
    await flow.result(old["scan1_command_code"], "SCAN1")
    await flow.scan("SCAN1", "A000000002-B", timestamp=30)
    async with flow.factory() as session:
        assert len(await flow.rows(session, "bin_passage")) == 2
        assert len(await flow.messages(session)) == 2
