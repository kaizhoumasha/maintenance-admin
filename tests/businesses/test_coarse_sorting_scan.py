"""Coarse scans persist only facts and pending work in the receiver's transaction."""

import pytest
from sqlalchemy import select

from app.models import CallbackReceipt, WorkLine


@pytest.mark.parametrize(
    "changes,omit",
    [
        ({}, ()),
        ({"result": "NG"}, ()),
        ({"reel_diameter": 15.5, "reel_thickness": 20.25}, ()),
    ],
)
async def test_scan_preserves_original_facts_and_pending_work(coarse_scan, changes, omit):
    scan = coarse_scan
    body = scan.event(changes=changes, omit=omit)
    outbound_before = await scan.outbound_counts()
    response = await scan.post(body)
    assert response.status_code == 200, response.text
    assert response.json() == {"code": 200, "message": "ACK"}
    rows = await scan.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["payload"] == body["data"]
    assert row["timestamp"] == body["timestamp"]
    assert row["device_code"] == scan.codes[0]
    assert row["device_role"] == "INPUT_ARM"
    assert row["work_line_id"] == scan.line_ids[0]
    assert row["pending"] is True
    assert row["wait_reason"] == "AWAITING_VALIDATION_STAGE"
    async with scan.factory() as session:
        receipt = await session.get(CallbackReceipt, row["receipt_id"])
        line = await session.get(WorkLine, scan.line_ids[0])
        assert row["event_digest"] == receipt.operation_id
        assert row["workline_code"] == line.line_code
        assert row["work_line_version"] == line.version
    assert await scan.outbound_counts() == outbound_before


@pytest.mark.parametrize(
    "changes,omit",
    [
        ({}, ("barcode",)),
        ({}, ("scan_time",)),
        ({}, ("reel_diameter",)),
        ({}, ("reel_thickness",)),
        ({}, ("result",)),
        ({"barcode": 123}, ()),
        ({"scan_time": True}, ()),
        ({"scan_time": -1}, ()),
        ({"scan_time": 2**63}, ()),
        ({"reel_diameter": True}, ()),
        ({"reel_thickness": "20"}, ()),
        ({"result": "UNKNOWN"}, ()),
    ],
)
async def test_invalid_scan_is_rejected_without_business_acceptance(coarse_scan, changes, omit):
    scan = coarse_scan
    response = await scan.post(scan.event(changes=changes, omit=omit))
    assert response.status_code == 400, response.text
    assert await scan.rows() == []
    async with scan.factory() as session:
        assert await session.scalar(select(CallbackReceipt.id)) is None


@pytest.mark.parametrize("timestamp,expected_count", [(1793206799000, 1), (1793206799001, 2)])
async def test_replay_reuses_acceptance_and_new_event_keeps_its_own_identity(
    coarse_scan, timestamp, expected_count
):
    scan = coarse_scan
    assert (await scan.post(scan.event())).status_code == 200
    first = await scan.rows()
    assert len(first) == 1
    assert (await scan.post(scan.event(timestamp=timestamp))).status_code == 200
    rows = await scan.rows()
    assert len(rows) == expected_count
    assert rows[0] == first[0]
    if expected_count == 2:
        assert rows[1]["payload"]["barcode"] == rows[0]["payload"]["barcode"]
        assert rows[1]["id"] != rows[0]["id"]
        assert rows[1]["event_digest"] != rows[0]["event_digest"]


async def test_storage_failure_rolls_back_receipt_scan_and_pending(coarse_scan):
    from sqlalchemy import event

    scan = coarse_scan
    engine = scan.factory.kw["bind"].sync_engine

    def fail_after_scan(connection, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and getattr(compiled.statement, "table", None) is scan.table:
            if compiled.statement.is_insert:
                raise RuntimeError("injected temporary scan storage failure")

    event.listen(engine, "after_cursor_execute", fail_after_scan)
    try:
        response = await scan.post(scan.event())
        assert response.status_code == 503
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_scan)
    assert await scan.rows() == []
    async with scan.factory() as session:
        assert await session.scalar(select(CallbackReceipt.id)) is None
    assert (await scan.post(scan.event())).status_code == 200
    assert len(await scan.rows()) == 1


@pytest.mark.parametrize("issue", ["wrong_business", "missing_position"])
async def test_business_configuration_rejection_is_diagnostic_only(coarse_scan, issue):
    from dataclasses import replace

    from app.businesses.definitions import PositionSlot

    scan = coarse_scan
    if issue == "wrong_business":
        async with scan.factory.begin() as session:
            line = await session.get(WorkLine, scan.line_ids[0])
            line.business_code = None
        reason = "WORKLINE_BUSINESS_MISMATCH"
    else:
        scan.runtime.definitions["coarse_sorting"] = replace(
            scan.definition,
            position_slots=(PositionSlot(slot_key="OUTLET", display_name="出料架位"),),
        )
        reason = "WORKLINE_CONFIGURATION_INCOMPLETE"
    response = await scan.post(scan.event())
    assert response.status_code == 200
    assert await scan.rows() == []
    async with scan.factory() as session:
        receipt = await session.scalar(select(CallbackReceipt))
        assert receipt.diagnostic == [{"reason_code": reason}]


async def test_extra_data_is_ignored_by_payload_but_retained_in_event_identity(coarse_scan):
    scan = coarse_scan
    body = scan.event()
    assert (await scan.post(body)).status_code == 200
    extra = scan.event(changes={"unused_measurement": "preserve-original-identity"})
    assert (await scan.post(extra)).status_code == 200
    rows = await scan.rows()
    assert len(rows) == 2
    assert rows[0]["payload"] == rows[1]["payload"] == body["data"]
    assert rows[0]["event_digest"] != rows[1]["event_digest"]


@pytest.mark.parametrize("field", ["reel_diameter", "reel_thickness"])
async def test_nonfinite_scan_measurement_is_rejected(coarse_scan, field):
    import json

    from httpx import ASGITransport, AsyncClient

    from app.main import app

    raw = json.dumps(coarse_scan.event(changes={field: float("inf")})).encode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/callback/event", content=raw, headers={"content-type": "application/json"}
        )
    assert response.status_code == 400
    assert await coarse_scan.rows() == []


async def test_scan_survives_public_receipt_cleanup_and_service_rebuild(coarse_scan):
    from sqlalchemy import delete, update

    scan = coarse_scan
    body = scan.event()
    assert (await scan.post(body)).status_code == 200
    original = (await scan.rows())[0]
    async with scan.factory.begin() as session:
        await session.execute(
            update(scan.table).values(pending=False, wait_reason="RETAINED_NEXT_STAGE_STATE")
        )
        await session.execute(
            delete(CallbackReceipt).where(CallbackReceipt.id == original["receipt_id"])
        )
    retained = (await scan.rows())[0]
    scan.install()
    replay = {**body, "unrelated_top_level": "ignored", "is_debug": True}
    assert (await scan.post(replay)).status_code == 200
    assert await scan.rows() == [retained]
    async with scan.factory() as session:
        assert await session.scalar(select(CallbackReceipt.id)) is not None


async def test_same_barcode_on_two_lines_has_separate_acceptance(coarse_scan):
    scan = coarse_scan
    for device in (0, 1):
        assert (await scan.post(scan.event(device=device))).status_code == 200
    rows = await scan.rows()
    assert len(rows) == 2
    assert {row["work_line_id"] for row in rows} == set(scan.line_ids)
    assert {row["device_code"] for row in rows} == set(scan.codes)
    assert rows[0]["payload"]["barcode"] == rows[1]["payload"]["barcode"]
    assert rows[0]["event_digest"] != rows[1]["event_digest"]


async def test_old_scan_does_not_follow_reassigned_device_after_receipt_cleanup(coarse_scan):
    from sqlalchemy import delete

    from app.models import Device

    scan = coarse_scan
    body = scan.event()
    assert (await scan.post(body)).status_code == 200
    original = (await scan.rows())[0]
    async with scan.factory.begin() as session:
        await session.execute(
            delete(CallbackReceipt).where(CallbackReceipt.id == original["receipt_id"])
        )
        old = await session.get(WorkLine, scan.line_ids[0])
        new = await session.get(WorkLine, scan.line_ids[1])
        device = await session.scalar(select(Device).where(Device.device_code == scan.codes[0]))
        old.device_bindings = {}
        new.device_bindings = {"INPUT_ARM": scan.codes[0]}
        old.version += 1
        new.version += 1
        device.work_line_id = new.id
        device.version += 1
    scan.install()
    assert (await scan.post(body)).status_code == 200
    assert await scan.rows() == [original]
    assert (await scan.post(scan.event(timestamp=1793206799001))).status_code == 200
    rows = await scan.rows()
    assert rows[0] == original
    assert rows[1]["work_line_id"] == scan.line_ids[1]
    assert rows[1]["device_code"] == original["device_code"]
    assert rows[1]["binding_revision"] != original["binding_revision"]


async def test_unregistered_ack_is_not_replayed_into_new_business_handler(coarse_scan):
    from app.integrations.ecs_callback import registry as events

    scan = coarse_scan
    events._registry.clear()
    body = scan.event()
    assert (await scan.post(body)).status_code == 200
    assert await scan.rows() == []
    scan.install()
    assert (await scan.post(body)).status_code == 200
    assert await scan.rows() == []
    assert (await scan.post(scan.event(timestamp=1793206799001))).status_code == 200
    assert len(await scan.rows()) == 1


@pytest.mark.parametrize("binding", ["unknown_device", "unbound_role"])
async def test_unmatched_scan_acks_without_creating_business_work(coarse_scan, binding):
    scan = coarse_scan
    body = scan.event()
    if binding == "unknown_device":
        body["device_code"] = "UNMATCHED_DEVICE"
    else:
        async with scan.factory.begin() as session:
            line = await session.get(WorkLine, scan.line_ids[0])
            line.device_bindings = {}
    assert (await scan.post(body)).status_code == 200
    assert await scan.rows() == []
    async with scan.factory() as session:
        receipt = await session.scalar(select(CallbackReceipt))
        assert receipt.diagnostic[0]["reason_code"] == "UNREGISTERED_EVENT_TYPE"


@pytest.mark.parametrize("rollback_first", [False, True])
async def test_concurrent_replays_wait_for_original_transaction(
    coarse_scan, wait_for_lock, rollback_first
):
    import asyncio

    scan = coarse_scan
    if scan.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency contract")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_first(ctx):
        if not entered.is_set():
            entered.set()
            await release.wait()
            if rollback_first:
                raise RuntimeError("first receiver transaction rolls back")

    scan.after_accept = hold_first
    first = asyncio.create_task(scan.post(scan.event()))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(scan.post(scan.event()))
        await wait_for_lock(second)
        assert not second.done()
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second is not None else []))
    assert [r.status_code for r in results] == ([503, 200] if rollback_first else [200, 200])
    rows = await scan.rows()
    assert len(rows) == 1 and rows[0]["pending"] is True
    async with scan.factory() as session:
        receipts = list(await session.scalars(select(CallbackReceipt)))
        assert len(receipts) == 1
        assert rows[0]["receipt_id"] == receipts[0].id
    assert (await scan.post(scan.event())).status_code == 200
    assert await scan.rows() == rows


async def test_configuration_update_waits_and_cannot_rewrite_accepted_work(
    coarse_scan, wait_for_lock
):
    import asyncio

    from sqlalchemy import update

    scan = coarse_scan
    if scan.factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock concurrency contract")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(ctx):
        entered.set()
        await release.wait()

    async def change_configuration():
        async with scan.factory.begin() as session:
            await session.execute(
                update(WorkLine)
                .where(WorkLine.id == scan.line_ids[0])
                .values(line_code="CHANGED_COARSE_LINE", version=WorkLine.version + 1)
            )

    scan.after_accept = hold
    receiving = asyncio.create_task(scan.post(scan.event()))
    changing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        changing = asyncio.create_task(change_configuration())
        await wait_for_lock(changing)
        assert not changing.done()
    finally:
        release.set()
        results = await asyncio.gather(receiving, *([changing] if changing is not None else []))
    assert results[0].status_code == 200
    original = (await scan.rows())[0]
    assert original["workline_code"] != "CHANGED_COARSE_LINE"
    scan.after_accept = None
    assert (await scan.post(scan.event(timestamp=1793206799001))).status_code == 200
    rows = await scan.rows()
    assert rows[0] == original
    assert rows[1]["workline_code"] == "CHANGED_COARSE_LINE"
    assert rows[1]["work_line_version"] == original["work_line_version"] + 1


def test_unlisted_coarse_event_cannot_register_on_first_import():
    import os
    import subprocess
    import sys

    code = """
from importlib import import_module
from app.businesses import catalog
from app.integrations.discovery import load_formal_modules
from app.integrations.ecs_callback.registry import events

catalog.entries = lambda: ()
load_formal_modules()
catalog.validate_business_contracts()
original_events = events()
try:
    import_module("app.businesses.coarse_sorting.ecs_event.scan_completed")
except ValueError as error:
    assert "catalog-owned formal ENTRY_MODULES" in str(error), str(error)
else:
    raise AssertionError("unlisted business event was registered")
assert events() == original_events
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "DATABASE_URL": "sqlite+aiosqlite://", "RELIABLE_TARGETS": "{}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
