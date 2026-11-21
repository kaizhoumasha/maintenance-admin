# ruff: noqa: F405
from tests.businesses.smt_auto._outbound_support import *  # noqa: F401,F403
from tests.businesses.smt_auto._outbound_support import _empty_pending_page, _TrackingFactory


async def test_pending_scan_closes_snapshot_before_advance(outbound_host, monkeypatch):
    from dataclasses import replace

    from app.businesses.smt_auto.service import SmtAutoService

    host = outbound_host
    service = SmtAutoService(
        replace(host.dispatch.capabilities, ecs=host.ecs),
        worklines=(),
    )
    service.dispatch = host.dispatch
    service.roller = host.roller
    service._pending = lambda _: {
        "TASK": select(AutoTask.task_id).where(
            AutoTask.status.in_(("QUEUED", "PREPARING", "EXECUTING"))
        )
    }

    async def no_upper(*args, **kwargs):
        return None

    monkeypatch.setattr(service.dispatch, "pending_upper_id", no_upper)
    monkeypatch.setattr(service.dispatch, "pending_snapshot_ids", no_upper)
    monkeypatch.setattr(service.roller, "get_pending_upper_id", no_upper)
    monkeypatch.setattr(service.roller, "pending_snapshot_ids", no_upper)
    tracker = _TrackingFactory(host.factory)
    service.capabilities = replace(service.capabilities, session_factory=tracker)
    visited = []
    active_at_advance = []

    async def advance(kind, record_id, **kwargs):
        visited.append(record_id)
        active_at_advance.append(tracker.active)
        async with host.factory.begin() as session:
            session.add(
                AutoTask(
                    task_id="a-late",
                    task_type="AUTO",
                    business_type="outbound",
                    work_line_id=1,
                    workline_code="LINE",
                    issued_operation_id="late-issued",
                    issued_timestamp=2,
                    issued_data={},
                    status="QUEUED",
                    queue_revision=1,
                    dispatch_sequence=2,
                    plan_revision=0,
                    pending=True,
                )
            )

    monkeypatch.setattr(service, "advance", advance)
    assert await service.scan_pending(limit=1) == 1
    assert visited == ["t"]
    assert active_at_advance == [0]


async def test_pending_scan_reads_candidates_in_bounded_pages():
    from datetime import UTC, datetime

    from sqlalchemy import Boolean, Column, MetaData, String, Table, event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.businesses.smt_auto.service import SmtAutoService

    metadata = MetaData()
    table = Table(
        "test_smt_auto_bounded_pending_scan",
        metadata,
        Column("task_id", String, primary_key=True),
        Column("pending", Boolean, nullable=False),
    )
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    task_ids = tuple(f"T{i:04}" for i in range(2505))
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with factory.begin() as session:
        await session.execute(
            table.insert(), [{"task_id": task_id, "pending": True} for task_id in task_ids]
        )

    service = SmtAutoService.__new__(SmtAutoService)
    service.wms = SimpleNamespace(clock=lambda: datetime.now(UTC))
    service.capabilities = SimpleNamespace(session_factory=factory)
    service._pending = lambda _: {"TASK": select(table.c.task_id).where(table.c.pending.is_(True))}
    empty = SimpleNamespace(
        pending_snapshot_ids=lambda *args, **kwargs: _empty_pending_page(),
    )
    service.dispatch = empty
    service.roller = empty
    visited = []

    async def advance(kind, record_id, **kwargs):
        visited.append(record_id)

    service.advance = advance
    statements = []

    def record_sql(*args):
        statements.append(args[2])

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        assert await service.scan_pending(limit=100) == len(task_ids)
        assert visited == list(task_ids)
        selects = [
            statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) == 4
        assert all("LIMIT" in statement.upper() for statement in selects)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()


@pytest.mark.parametrize(
    "decided_outbound",
    ["PART-EXAMPLE,Q10000,MODEL-EXAMPLE,D260505,LOT-EXAMPLE,SREEL-197"],
    indirect=True,
)
@pytest.mark.parametrize(
    "delivery", ["unsent", "claimed", "in_flight_then_unavailable", "accepted", "put_sent"]
)
@pytest.mark.parametrize("direction_host", ["sqlite", "postgres"], indirect=True)
async def test_scan_revision_replaces_only_unaccepted_material_once(decided_outbound, delivery):
    host = decided_outbound
    barcode = "PART-EXAMPLE,Q10000,MODEL-EXAMPLE,D260505,LOT-EXAMPLE,SREEL-176"
    operation = "outbound.bin.material.decide@v1"
    accept = {
        "result": "ACCEPT",
        "target_locator": {
            "type": "RACK_SLOT",
            "rack_id": "target",
            "rack_face": "A",
            "slot_id": "A1",
            "rack_layer": 1,
            "rack_column": 1,
        },
    }
    if delivery in ("accepted", "put_sent"):
        await host.settle(operation, accept)
        if delivery == "put_sent":
            async with host.factory.begin() as session:
                plate = await host.outbound.advance(
                    session, work_line_id=1, plate_id=host.plate_id, now_ms=59
                )
                assert plate.put_command_code
                put_code = plate.put_command_code
            await host.arm_ack(put_code)
    store = MessageStore(host.factory, records=history_records)
    if delivery in ("claimed", "in_flight_then_unavailable"):
        (claim,) = await store.claim(1, host.requests.outbox.targets)
    if delivery == "in_flight_then_unavailable":
        assert await store.before_send(claim, host.requests.outbox.targets[claim.message.target])
    async with host.factory() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        original_pick = plate.pick_command_code
    receipt = await host.platform_scan(
        original_pick, barcode=barcode, timestamp=105, scan_revision=2
    )
    assert receipt.status_code == 200
    if delivery == "claimed":
        recovery = MessageStore(
            host.factory,
            records=history_records,
            clock=lambda: claim.lease_until + timedelta(seconds=1),
        )
        assert await recovery.recover_expired(1, host.requests.outbox.targets) == 0
        assert not await recovery.claim(1, host.requests.outbox.targets)
        assert (
            await store.before_send(claim, host.requests.outbox.targets[claim.message.target])
            is None
        )
    if delivery == "in_flight_then_unavailable":
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=59
            )
            assert plate.barcode.endswith("197") and plate.put_command_code is None
        response = HttpResult(
            DeliveryState.HTTP_RESPONSE_COMPLETE,
            None,
            503,
            (),
            json.dumps(
                dict(
                    operation_id=claim.message.operation_id,
                    code="UNAVAILABLE",
                    timestamp=105,
                    data={},
                )
            ).encode(),
        )
        verdict = ADAPTERS["wms-decisions"].classify_response(claim.message, response)
        assert await store.finish(
            claim,
            AttemptOutcome(
                MessageStatus.PENDING,
                result=response,
                verdict=verdict,
                next_attempt_at=store.clock() + timedelta(seconds=1),
                not_received_count=1,
            ),
        )
    if delivery in ("unsent", "claimed", "in_flight_then_unavailable"):
        later_store = MessageStore(
            host.factory,
            records=history_records,
            clock=lambda: store.clock() + timedelta(seconds=2),
        )
        assert not await later_store.claim(1, host.requests.outbox.targets)
    for now in (60, 61):
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=now)
        repeated = await host.platform_scan(
            original_pick, barcode=barcode, timestamp=105, scan_revision=2
        )
        assert repeated.status_code == 200 and repeated.outcome == "duplicate"
    if delivery in ("unsent", "claimed", "in_flight_then_unavailable"):
        stale = await host.platform_scan(
            original_pick, barcode=barcode[:-3] + "197", timestamp=106, scan_revision=1
        )
        assert stale.diagnostic[0]["reason_code"] == "PLATFORM_SCAN_STALE_REVISION"
    async with host.factory() as session:
        rounds = (
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(AutoOutboundDecisionRound.plate_id == host.plate_id)
                .order_by(AutoOutboundDecisionRound.id)
            )
        ).all()
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        old_message = await session.get(ReliableMessage, rounds[0].message_id)
        assert rounds[0].request_data["barcode"].endswith("197")
        old_request = json.loads(await read_history_body(session, old_message.request_record_id))
        assert old_request["data"]["barcode"].endswith("197")
        if delivery in ("accepted", "put_sent"):
            assert len(rounds) == 1 and old_message.status == "DONE"
            assert plate.barcode.endswith("197")
            assert plate.source_arrival_snapshot["pending_platform_scan"]["barcode"] == barcode
            assert plate.put_command_code == (put_code if delivery == "put_sent" else None)
        else:
            assert len(rounds) == 2 and old_message.status == "ABANDONED"
            assert not rounds[0].unresolved
            assert rounds[1].operation_id != rounds[0].operation_id
            assert rounds[1].request_data["barcode"] == plate.barcode == barcode
            assert rounds[1].request_data["scanned_at"] == 105
            new_message = await session.get(ReliableMessage, rounds[1].message_id)
            new_request = json.loads(
                await read_history_body(session, new_message.request_record_id)
            )
            assert new_request["data"]["barcode"] == barcode
            assert plate.put_command_code is None
            scan_id = plate.source_arrival_snapshot["pick_command"]["platform_scan_event_id"]
            assert (
                await session.scalar(
                    select(IntegrationExchange.receipt_id).where(
                        IntegrationExchange.receipt_id == scan_id,
                        IntegrationExchange.application_ref["predecessor_event_id"]
                        .as_integer()
                        .is_not(None),
                    )
                )
                == scan_id
            )
    if delivery in ("unsent", "claimed", "in_flight_then_unavailable"):
        from app.businesses.smt_auto.outbound_racks import OutboundRacks
        from app.businesses.smt_auto.task_flow import TaskFlow

        flow = TaskFlow(
            host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
        )
        racks = OutboundRacks(host.outbound, station_id="station")
        await host.settle(operation, accept)
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=62
            )
            assert plate.put_command_code and plate.final_decision_round_id == rounds[1].id
            put_code = plate.put_command_code
            rack_job_id = plate.target_rack_job_id
        await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
        async with host.factory.begin() as session:
            await consume_bin_plan(host, session, now_ms=63)
            work = await session.get(AutoBinWork, host.work_id)
            assert (
                await flow.release_bin(
                    session, work_line_id=1, arrival_id=work.arrival_id, now_ms=63
                )
                == "RELEASING"
            )
            release_code = work.release_command_code
            arrival_id = work.arrival_id
        assert (await host.result(release_code)).status_code == 200
        assert (await host.arm_result(put_code)).status_code == 200
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=64)
            await flow.consume_release(session, work_line_id=1, arrival_id=arrival_id, now_ms=65)
        await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
        async with host.factory.begin() as session:
            await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=66)
            await racks.advance(session, task_id="t", now_ms=67)
        await host.settle(
            "rack.departure_decide@v1",
            {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "SMT_B"}},
        )
        async with host.factory.begin() as session:
            await racks.advance(session, task_id="t", now_ms=68)
            await racks.advance(session, task_id="t", now_ms=69)
            rack = await host.dispatch.progress(session, job_id=rack_job_id)
            assert rack.actions[-1].kind == "DEPART"


async def test_material_notice_targets_original_outbound_operation(decided_outbound, monkeypatch):
    host = decided_outbound
    from app.businesses.smt_auto.service import SmtAutoService
    from app.tasks import business_tasks

    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", lambda *args: notices.append(args))
    await host.settle(
        "outbound.bin.material.decide@v1",
        {
            "result": "ACCEPT",
            "target_locator": {
                "type": "RACK_SLOT",
                "rack_id": "target",
                "rack_face": "A",
                "slot_id": "A1",
                "rack_layer": 1,
                "rack_column": 1,
            },
        },
        on_delivered=business_wakeups.wms_delivery_committed,
    )
    async with host.factory() as session:
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound).where(
                AutoOutboundDecisionRound.plate_id == host.plate_id
            )
        )
        assert notices == [("smt_auto", round_.operation, round_.operation_id)]
    service = SmtAutoService(replace(host.dispatch.capabilities, ecs=host.ecs), worklines=())
    service.dispatch, service.roller, service.outbound = host.dispatch, host.roller, host.outbound

    def unexpected_sweep(_):
        raise AssertionError("ACCEPT must bypass the pending sweep")

    monkeypatch.setattr(service, "_pending", unexpected_sweep)
    monkeypatch.setattr(
        business_tasks,
        "_runtime",
        lambda *_: SimpleNamespace(progressions={"smt_auto": service.scan_pending}),
    )
    assert await business_tasks.advance_pending(host.factory, *notices[0]) == 1
    async with host.factory() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        assert plate.put_command_code


@pytest.mark.parametrize("direction_host", ["postgres"], indirect=True)
@pytest.mark.parametrize(
    "decided_outbound",
    ["PART-EXAMPLE,Q10000,MODEL-EXAMPLE,D260505,LOT-EXAMPLE,SREEL-197"],
    indirect=True,
)
async def test_targeted_material_and_sweep_create_one_put(decided_outbound, monkeypatch, caplog):
    from app.businesses.smt_auto.service import SmtAutoService

    host = decided_outbound
    async with host.factory() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        original_pick = plate.pick_command_code
    correction = "PART-EXAMPLE,Q10000,MODEL-EXAMPLE,D260505,LOT-EXAMPLE,SREEL-176"
    assert (
        await host.platform_scan(original_pick, barcode=correction, timestamp=105, scan_revision=2)
    ).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=60)
    assert (
        await host.platform_scan(original_pick, barcode=correction, timestamp=105, scan_revision=2)
    ).outcome == "duplicate"
    capabilities = replace(host.dispatch.capabilities, ecs=host.ecs)
    service = SmtAutoService(capabilities, worklines=())
    service.dispatch, service.roller, service.outbound = host.dispatch, host.roller, host.outbound
    monkeypatch.setattr(
        service,
        "_pending",
        lambda _: {"OUTBOUND_PLATE": select(AutoOutboundPlate.id).where(AutoOutboundPlate.pending)},
    )
    await host.settle(
        "outbound.bin.material.decide@v1",
        {
            "result": "ACCEPT",
            "target_locator": {
                "type": "RACK_SLOT",
                "rack_id": "target",
                "rack_face": "A",
                "slot_id": "A1",
                "rack_layer": 1,
                "rack_column": 1,
            },
        },
    )
    async with host.factory() as session:
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound).where(
                AutoOutboundDecisionRound.plate_id == host.plate_id,
                AutoOutboundDecisionRound.unresolved,
            )
        )
        assert round_.request_data["barcode"] == correction
        message = await session.get(ReliableMessage, round_.message_id)
        accepted_at = await session.scalar(
            select(IntegrationRecord.recorded_at).where(
                IntegrationRecord.exchange_id == message.exchange_id,
                IntegrationRecord.record_kind == "HTTP_RESPONSE_OBSERVED",
                IntegrationRecord.http_status == 200,
            )
        )
        original_count = await session.scalar(
            select(func.count())
            .select_from(IntegrationExchange)
            .where(IntegrationExchange.role == "ECS_COMMAND")
        )

    both_started = asyncio.Event()
    kinds = set()
    advance = service.advance

    async def advance_together(kind, record_id, **kwargs):
        if kind in ("OUTBOUND_DECISION", "OUTBOUND_PLATE"):
            kinds.add(kind)
            if len(kinds) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
        return await advance(kind, record_id, **kwargs)

    monkeypatch.setattr(service, "advance", advance_together)
    notified_at_ms = time_ns() // 1_000_000
    with caplog.at_level("INFO", logger="app.businesses.smt_auto.transactions"):
        await asyncio.gather(
            service.scan_pending(
                operation=round_.operation,
                operation_id=round_.operation_id,
                notified_at_ms=notified_at_ms,
            ),
            service.scan_pending(),
        )
    async with host.factory() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        assert plate.put_command_code
        registered_at = await session.scalar(
            select(IntegrationExchange.created_at).where(
                IntegrationExchange.command_code == plate.put_command_code
            )
        )
        assert registered_at >= accepted_at
        logging.getLogger(__name__).info(
            "Isolated KT14 ACCEPT to ARM04 registration %.3f ms",
            (registered_at - accepted_at).total_seconds() * 1000,
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == original_count + 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.bin_work_id == host.work_id,
                    AutoOutboundDecisionRound.operation == "outbound.bin.work_plan@v1",
                    AutoOutboundDecisionRound.normal_continuation.is_(True),
                )
            )
            == 1
        )
    for field in (
        "preceding_ms",
        "workline_lock_ms",
        "related_locks_ms",
        "processing_ms",
        "commit_ms",
    ):
        assert field in caplog.text
    assert plate.put_command_code in caplog.text
    await host.arm_ack(plate.put_command_code)
    async with host.factory() as session:
        command = await host.ecs.get_command(
            session, owner=CommandOwner("smt_auto"), command_code=plate.put_command_code
        )
        assert command.ack_received and command.status == "WAITING_RESULT"


@pytest.mark.parametrize(
    "tail", ["unknown", "WAIT", "ready_first", "ack_first", "put_first", "put_failed"]
)
async def test_ready_return_rack_yields_only_to_bin_successor(decided_outbound, tail, monkeypatch):
    host = decided_outbound
    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    _, _, rack_plate_id = await return_face(host)
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = first.put_command_code
        assert put_code
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=61
        )
        assert rack.pick_command_code is None
    if tail in ("ack_first", "put_failed"):
        await host.arm_ack(put_code, on_ack=business_wakeups.ecs_ack_committed)
    if tail == "put_first":
        assert (await host.arm_result(put_code)).status_code == 200

    if tail != "unknown":
        result = {"result": "WAIT", "retry_after_ms": 1000} if tail == "WAIT" else ready_bin()
        await host.settle("outbound.bin.work_plan@v1", result)
    successor_id = None
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=62)
        successor = await session.scalar(
            select(AutoOutboundPlate).where(
                AutoOutboundPlate.bin_work_id == host.work_id,
                AutoOutboundPlate.id != host.plate_id,
            )
        )
        if tail in ("unknown", "WAIT"):
            assert successor is None
        else:
            assert successor is not None
            successor_id = successor.id
            await host.outbound.advance(session, work_line_id=1, plate_id=successor_id, now_ms=63)
            assert bool(successor.pick_command_code) is (tail != "ready_first")

    if tail in ("unknown", "WAIT", "ready_first"):
        await host.arm_ack(put_code, on_ack=business_wakeups.ecs_ack_committed)
    assert notices == ([] if tail == "put_first" else ["smt_auto"])
    async with host.factory.begin() as session:
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=64
        )
        assert rack.pick_command_code is None
        if successor_id is not None:
            successor = await host.outbound.advance(
                session, work_line_id=1, plate_id=successor_id, now_ms=65
            )
            pick_code = successor.pick_command_code
            assert pick_code
            await host.outbound.advance(session, work_line_id=1, plate_id=successor_id, now_ms=66)
            assert successor.pick_command_code == pick_code
            put = await host.ecs.get_command(
                session, owner=CommandOwner("smt_auto"), command_code=put_code
            )
            assert put.status == ("SUCCEEDED" if tail == "put_first" else "WAITING_RESULT")
    if tail == "put_failed":
        assert (
            await host.arm_result(put_code, status="FAILED", empty="PUT_FAILED")
        ).status_code == 200
        async with host.factory.begin() as session:
            rack = await host.outbound.advance(
                session, work_line_id=1, plate_id=rack_plate_id, now_ms=67
            )
            successor = await host.outbound.advance(
                session, work_line_id=1, plate_id=successor_id, now_ms=68
            )
            assert rack.pick_command_code is None
            assert successor.pick_command_code == pick_code
            assert not await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.plate_id == successor_id,
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE",
                )
            )
        return
    if successor_id is not None:
        assert (await host.arm_result(pick_code)).status_code == 200
        assert (
            await host.platform_scan(pick_code, barcode="next reel", timestamp=105)
        ).status_code == 200
        async with host.factory.begin() as session:
            successor = await host.outbound.advance(
                session, work_line_id=1, plate_id=successor_id, now_ms=67
            )
            assert successor.barcode == "next reel"
            round_id = await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.plate_id == successor_id,
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE",
                )
            )
            assert bool(round_id) is (tail == "put_first")
    if tail != "put_first":
        assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=70
        )
        assert bool(rack.pick_command_code) is (successor_id is None)
        if successor_id is not None:
            successor = await host.outbound.advance(
                session, work_line_id=1, plate_id=successor_id, now_ms=71
            )
            assert successor.pick_command_code == pick_code
            assert await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.plate_id == successor_id,
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE",
                )
            )
        else:
            work = await session.get(AutoBinWork, host.work_id)
            assert not work.release_command_code
            assert not await host.outbound.bin_closed(session, work, for_release=True)


@pytest.mark.parametrize("tail", ["unknown", "WAIT"])
async def test_ready_bin_takes_platform_after_rack_put_with_tail_pending(outbound_host, tail):
    host = outbound_host
    _, member_id, rack_plate_id = await return_face(host)
    async with host.factory.begin() as session:
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=50
        )
        rack_pick = rack.pick_command_code
        assert rack_pick
    assert (await host.arm_result(rack_pick, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=rack_plate_id, now_ms=60)
    await host.settle("outbound.rackslot.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        rack = await host.outbound.advance(
            session, work_line_id=1, plate_id=rack_plate_id, now_ms=70
        )
        rack_put = rack.put_command_code
        assert rack_put
    if tail == "WAIT":
        await host.settle(
            "outbound.return_rack.work_plan@v1", {"result": "WAIT", "retry_after_ms": 1000}
        )
    await host.settle("outbound.bin.work_plan@v1", ready_bin())
    await host.arm_ack(rack_put)
    async with host.factory.begin() as session:
        await host.outbound.work_plans.consume_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=72
        )
        await consume_bin_plan(host, session, now_ms=72)
        bin_plate = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.bin_work_id == host.work_id)
        )
        bin_plate_id = bin_plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=bin_plate_id, now_ms=73)
        assert bin_plate.pick_command_code is None
    assert (await host.arm_result(rack_put)).status_code == 200
    async with host.factory.begin() as session:
        bin_plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=bin_plate_id, now_ms=74
        )
        assert bin_plate.pick_command_code
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert not await host.outbound.face_source_terminal(session, member)
        rack_plate = await session.get(AutoOutboundPlate, rack_plate_id)
        assert rack_plate.report_consumed_at_ms is None


@pytest.mark.parametrize("rack_ready", [False, True])
async def test_new_bin_yields_to_ready_rack_only(outbound_host, rack_ready):
    host = outbound_host
    _, _, rack_plate_id = await return_face(host, ready=rack_ready)
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=41)
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C1",
            cell_index=1,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        ),
    )
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=42)
        plate_id = await session.scalar(
            select(AutoOutboundPlate.id).where(AutoOutboundPlate.bin_work_id == host.work_id)
        )
        bin_plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=plate_id, now_ms=43
        )
        assert bool(bin_plate.pick_command_code) is not rack_ready
        if rack_ready:
            rack = await host.outbound.advance(
                session, work_line_id=1, plate_id=rack_plate_id, now_ms=44
            )
            assert rack.pick_command_code


async def test_bin_tail_wait_retries_once_with_original_arrival(decided_outbound):
    host = decided_outbound
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=60)
    await host.settle("outbound.bin.work_plan@v1", {"result": "WAIT", "retry_after_ms": 20})
    async with host.factory.begin() as session:
        tail = await consume_bin_plan(host, session, now_ms=61)
        original_id = tail.operation_id
        request = tail.request_data
        work = await session.get(AutoBinWork, host.work_id)
        args = dict(work_line_id=1, arrival_id=work.arrival_id)
        plan = await host.work_plans.start_bin(session, **args, now_ms=80)
        assert plan.operation_id == original_id
        plan = await host.work_plans.start_bin(session, **args, now_ms=81)
        next_id = plan.operation_id
        assert next_id != original_id and plan.request_data == request
        again = await host.work_plans.start_bin(session, **args, now_ms=82)
        assert again.operation_id == next_id
    await host.settle("outbound.bin.work_plan@v1", ready_bin(), operation_id=next_id)
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=83)
        await consume_bin_plan(host, session, now_ms=84)
        assert await session.scalar(select(func.count()).select_from(AutoOutboundPlate)) == 2


async def test_accepted_bin_resumes_work_plan_after_source_is_reenabled(decided_outbound):
    host = decided_outbound
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.is_enabled = False
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.final_decision_round_id and plate.put_command_code is None
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.is_enabled = True
        work = await session.get(AutoBinWork, host.work_id)
        args = dict(work_line_id=1, arrival_id=work.arrival_id)
        round_ = await host.work_plans.start_bin(session, **args, now_ms=61)
        assert round_.unresolved
        assert (await host.work_plans.start_bin(session, **args, now_ms=62)).id == round_.id
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=63)
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=64
        )
        assert plate.put_command_code


@pytest.mark.parametrize("tail", ["unknown", "WAIT", "READY", "NO_WORK"])
async def test_scan2_release_follows_source_terminal_before_put_result(decided_outbound, tail):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = decided_outbound
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        release = dict(work_line_id=1, arrival_id=work.arrival_id)
        assert await flow.release_bin(session, **release, now_ms=55) == "WAITING"
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = plate.put_command_code
        assert put_code and plate.report_message_id is None
        assert await flow.release_bin(session, **release, now_ms=61) == "WAITING"
    if tail != "unknown":
        data = ready_bin() if tail == "READY" else {"result": tail}
        if tail == "WAIT":
            data["retry_after_ms"] = 1000
        await host.settle("outbound.bin.work_plan@v1", data)
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(session, **release, now_ms=62)
        state = "RELEASING" if tail == "NO_WORK" else "WAITING"
        assert await flow.release_bin(session, **release, now_ms=63) == state
        command = work.release_command_code
        assert bool(command) == (tail == "NO_WORK")
        assert await flow.release_bin(session, **release, now_ms=64) == state
        assert work.release_command_code == command
        assert await flow.advance(session, task_id="t", now_ms=65) == "WAITING"
    if command:
        assert (await host.result(command)).status_code == 200
        async with host.factory.begin() as session:
            assert await flow.release_bin(session, **release, now_ms=66) == "RELEASED"
            assert await flow.advance(session, task_id="t", now_ms=67) == "WAITING"
        assert (await host.arm_result(put_code)).status_code == 200
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=110
            )
            assert plate.report_message_id


async def test_second_bin_gets_own_work_plan_after_actual_arrival(decided_outbound):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = decided_outbound
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = plate.put_command_code
        assert put_code
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=61)
        first = await session.get(AutoBinWork, host.work_id)
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=first.arrival_id, now_ms=62)
            == "RELEASING"
        )
        release_code = first.release_command_code
    assert (await host.result(release_code)).status_code == 200
    async with host.factory.begin() as session:
        assert not await session.scalar(
            select(AutoBinWork.id).where(AutoBinWork.id != host.work_id)
        )
    new_bin = await host.scan(timestamp=111, bin_code="A000000002-C")
    assert new_bin is not None
    await host.settle("outbound.bin.work_plan@v1", ready_bin())
    async with host.factory.begin() as session:
        round_ = await consume_bin_plan(host, session, now_ms=112, work_id=new_bin.id)
        assert round_.request_data == {
            "task_id": "t",
            "bin_code": "A000000002",
            "scanned_at": 111,
        }
        next_plate = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.bin_work_id == new_bin.id)
        )
        next_id = next_plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=next_id, now_ms=113)
        assert next_plate.pick_command_code is None
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        next_plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=next_id, now_ms=114
        )
        assert next_plate.pick_command_code
        first_plate = await session.get(AutoOutboundPlate, host.plate_id)
        assert first_plate.report_consumed_at_ms is None


@pytest.mark.parametrize("changed", ["scanner", "rack_arm", "transfer_point", "rack", "face"])
async def test_put_reuses_current_arrival_after_unrelated_line_change(decided_outbound, changed):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="other" if changed == "rack" else "target",
                rack_face="B" if changed == "face" else "A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    async with host.factory.begin() as session:
        line = await session.get(WorkLine, 1)
        line.version += 1
        host.outbound.settings[1] = replace(
            host.outbound.settings[1], work_line_version=line.version
        )
        if changed == "scanner":
            line.device_bindings = {**line.device_bindings, "PLATFORM_SCANNER": "other-scanner"}
        elif changed == "rack_arm":
            arm = await session.scalar(
                select(Device).where(Device.device_code == line.device_bindings["RACK_ARM"])
            )
            arm.version += 1
        elif changed == "transfer_point":
            line.position_bindings = {**line.position_bindings, "TRANSFER_RACK": "Q"}
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert (plate.put_command_code is not None) == (changed == "scanner")
        if plate.put_command_code:
            job = await host.dispatch.progress(session, job_id=plate.target_rack_job_id)
            assert job.origin.resources["work_line_version"] == 1
            assert (
                plate.source_arrival_snapshot["put_command"]["device_code"]
                == line.device_bindings["RACK_ARM"]
            )


async def test_material_accept_with_legacy_successor_cannot_move_current_plate(decided_outbound):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        {**accepted_target(), "next_source_action": ready_bin()},
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code is None
        assert plate.final_decision_round_id is None
        assert await session.scalar(select(func.count()).select_from(AutoOutboundPlate)) == 1


@pytest.mark.parametrize("result", ["REJECT", "WAIT"])
async def test_conflicting_material_decision_cannot_move_or_reschedule_plate(
    decided_outbound, result
):
    host = decided_outbound
    data = (
        dict(
            business_exception_code="MATERIAL_REJECTED",
            source_disposition="CLOSE",
            ng_locator=dict(type="NG_ZONE", zone_code="MATERIAL_NG_01"),
        )
        if result == "REJECT"
        else dict(retry_after_ms=1)
    )
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(result=result, target_locator=accepted_target()["target_locator"], **data),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.final_decision_round_id is None
        assert plate.put_command_code is None
        original = await session.scalar(
            select(AutoOutboundDecisionRound).where(AutoOutboundDecisionRound.plate_id == plate.id)
        )
        assert original.unresolved and original.consumed_at_ms is None


@pytest.mark.parametrize("rescan", [False, True])
async def test_target_rotation_requires_original_applied_action(decided_outbound, rescan):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_preparation="ROTATE",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="B",
                slot_id="B1",
                rack_layer=2,
                rack_column=3,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code is None
        assert plate.final_decision_round_id is not None
        await racks.advance(session, task_id="t", now_ms=70)
        assert plate.target_preparation_action_id
        job = await host.dispatch.progress(session, job_id=plate.target_rack_job_id)
        action = next(a for a in job.actions if a.id == plate.target_preparation_action_id)
        assert action.kind == "ROTATE"
        assert not await host.outbound.target_clear(session, job_id=plate.target_rack_job_id)
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=80)
        assert plate.put_command_code is None
        await apply_outcome(
            session, action, "SUCCEEDED", point="P", rack_id="target", arrival_face="B"
        )
        await racks.advance(session, task_id="t", now_ms=90)
        assert not await host.outbound.target_clear(session, job_id=plate.target_rack_job_id)
        pick_code = plate.pick_command_code
    if rescan:
        assert (
            await host.platform_scan(pick_code, barcode="corrected", timestamp=105, scan_revision=2)
        ).status_code == 200
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=100
        )
        assert bool(plate.put_command_code) is not rescan


@pytest.mark.parametrize("decided_outbound", ["NoRead"], indirect=True)
@pytest.mark.parametrize("direction_host", ["sqlite", "postgres"], indirect=True)
async def test_bin_waits_for_readable_platform_scan(decided_outbound):
    host = decided_outbound
    async with host.factory() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        code = plate.pick_command_code
        operation = "outbound.bin.material.decide@v1"
        requests = select(ReliableMessage).where(ReliableMessage.operation == operation)
        assert await session.scalar(requests) is None
        assert plate.pending and plate.put_command_code is None

    assert (
        await host.platform_scan(code, barcode=" NOREAD ", timestamp=89, scan_revision=2)
    ).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=90)
        assert await session.scalar(requests) is None

    for now in (92, 93):
        assert (
            await host.platform_scan(code, barcode="REEL-BIN-1", timestamp=91, scan_revision=3)
        ).status_code == 200
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=now)

    assert (
        await host.platform_scan(code, barcode="NoRead", timestamp=94, scan_revision=4)
    ).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=95)
    async with host.factory() as session:
        (message,) = (await session.scalars(requests)).all()
        assert message.pause_reason is None
        request = json.loads(await read_history_body(session, message.request_record_id))
        assert request["data"]["barcode"] == "REEL-BIN-1"
        assert request["data"]["scanned_at"] == 91


@pytest.mark.parametrize("unread_source", ["event", "result"])
@pytest.mark.parametrize("direction_host", ["sqlite", "postgres"], indirect=True)
async def test_rackslot_waits_for_readable_platform_scan(outbound_host, unread_source):
    host = outbound_host
    _, _, plate_id = await return_face(host)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        code = plate.pick_command_code
    if unread_source == "event":
        assert (await host.platform_scan(code, barcode="NoRead")).status_code == 200
        assert (await host.arm_result(code)).status_code == 200
    else:
        assert (await host.arm_result(code, barcode="NoRead", scanned_at=88)).status_code == 200

    operation = "outbound.rackslot.material.decide@v1"
    requests = select(ReliableMessage).where(ReliableMessage.operation == operation)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=90)
        assert await session.scalar(requests) is None
        assert plate.pending and plate.put_command_code is None

    revision = 2 if unread_source == "event" else 1
    assert (
        await host.platform_scan(code, barcode="REEL-RACK-1", timestamp=91, scan_revision=revision)
    ).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=92)
        (message,) = (await session.scalars(requests)).all()
        request = json.loads(await read_history_body(session, message.request_record_id))
        assert request["data"]["barcode"] == "REEL-RACK-1"
        assert request["data"]["scanned_at"] == 91


@pytest.mark.parametrize("decided_outbound", ["UNRECOGNIZED-REEL"], indirect=True)
@pytest.mark.parametrize(
    ("placed", "disposition", "exception_code", "rescan", "reconciled"),
    [
        (False, "CLOSE", "MATERIAL_REJECTED", False, False),
        (True, "CONTINUE", "BARCODE_INVALID", False, False),
        (True, "CONTINUE", "BARCODE_INVALID", True, False),
        (True, "CONTINUE", "BARCODE_INVALID", True, True),
        (True, "CLOSE", "BARCODE_INVALID", True, True),
        (True, "CONTINUE", "SOURCE_CELL_MISMATCH", False, False),
        (True, "CLOSE", "SOURCE_CELL_MISMATCH", False, False),
    ],
)
async def test_rejected_plate_uses_line_bin_arm_and_requires_original_ng_put(
    decided_outbound, placed, disposition, exception_code, rescan, reconciled, monkeypatch
):
    host = decided_outbound
    operation = "outbound.bin.material.decide@v1"
    await host.settle(
        operation,
        dict(
            result="REJECT",
            business_exception_code=exception_code,
            source_disposition=disposition,
            detailMessage="OK",
            target_locator=None,
            ng_locator=dict(type="NG_ZONE", zone_code="MATERIAL_NG_01", label="NG"),
        ),
    )
    if rescan:
        async with host.factory() as session:
            plate = await session.get(AutoOutboundPlate, host.plate_id)
            pick_code = plate.pick_command_code
            original = await session.scalar(
                select(ReliableMessage).where(ReliableMessage.operation == operation)
            )
            original_bodies = (
                await read_history_body(session, original.request_record_id),
                await read_history_body(session, original.terminal_response_record_id),
            )
        assert (
            await host.platform_scan(pick_code, barcode="rescanned", timestamp=105, scan_revision=2)
        ).status_code == 200
        async with host.factory() as session:
            plate = await session.get(AutoOutboundPlate, host.plate_id)
            correction = plate.source_arrival_snapshot["pending_platform_scan"]
            assert correction["revision"] == 2 and correction["barcode"] == "rescanned"
    async with host.factory.begin() as session:
        for now in (60, 70):
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now
            )
        assert plate.final_decision_round_id
        decision = await session.get(AutoOutboundDecisionRound, plate.final_decision_round_id)
        assert decision.response_snapshot["data"]["business_exception_code"] == exception_code
        assert plate.put_command_code
        code = plate.put_command_code
        line = await session.get(WorkLine, 1)
        assert (
            plate.source_arrival_snapshot["put_command"]["device_code"]
            == line.device_bindings["BIN_ARM"]
        )
        assert plate.source_arrival_snapshot["put_command"]["params"] == dict(
            source=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
            target=dict(location_id="STATION_NG", location_type="SCAN_NG"),
        )
        assert plate.report_message_id is None
        assert plate.pending
        assert not await host.outbound.closed(session, plate)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )

    if placed:
        assert (await host.arm_result(code)).status_code == 200
    else:
        assert (await host.arm_result(code, status="FAILED", empty="PUT_FAILED")).status_code == 200
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=80
        )
        assert plate.put_command_code == code
        if not placed:
            assert plate.pending and plate.report_message_id is None
            assert not await host.outbound.closed(session, plate)
            return
        assert plate.report_data["to_locator"] == dict(type="NG_ZONE", zone_code="MATERIAL_NG_01")
        assert plate.report_data["barcode"] == "UNRECOGNIZED-REEL"
        assert plate.report_data["occurred_at"] == 99
        assert not await host.outbound.closed(session, plate)
    if reconciled:
        from app.businesses.smt_auto.service import SmtAutoService

        auto = SmtAutoService(replace(host.dispatch.capabilities, ecs=host.ecs), worklines=())
        auto.dispatch, auto.roller, auto.outbound = host.dispatch, host.roller, host.outbound
        work = set()

        async def record_advance(kind, record_id, **_):
            work.add((kind, record_id))

        monkeypatch.setattr(auto, "advance", record_advance)
        await auto.scan_pending()
        assert ("OUTBOUND_REPORT", host.plate_id) in work
        work.clear()
    if not reconciled:
        await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        plate = await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=90)
        if reconciled:
            plate.physical_closure_basis = dict(
                kind="MANUAL_NG", command_code=code, round_id=plate.final_decision_round_id
            )
            plate.direct_obligations_closed_at_ms = 90
            assert not await host.outbound.closed(session, plate)
            report = await session.get(ReliableMessage, plate.report_message_id)
            report.status = MessageStatus.ABANDONED
            report.next_attempt_at = None
            await session.flush()
            assert plate.report_consumed_at_ms is None and plate.report_response is None
            if disposition == "CLOSE":
                assert not await host.outbound.closed(session, plate)
                plate.source_closed_basis = dict(
                    kind="NG_CLOSE", round_id=plate.final_decision_round_id
                )
            report_data = plate.report_data
            plate.report_data = {**report_data, "barcode": "another-plate"}
            assert not await host.outbound.closed(session, plate)
            plate.report_data = report_data
        assert await host.outbound.closed(session, plate)
        assert plate.pending
        if rescan:
            assert plate.source_arrival_snapshot["pending_platform_scan"] == correction
            assert plate.barcode == "UNRECOGNIZED-REEL"
            (message,) = await session.scalars(
                select(ReliableMessage).where(ReliableMessage.operation == operation)
            )
            assert (message.id, message.status, message.attempts) == (
                original.id,
                original.status,
                original.attempts,
            )
            assert (
                await read_history_body(session, message.request_record_id),
                await read_history_body(session, message.terminal_response_record_id),
            ) == original_bodies

        assert (plate.source_closed_basis is not None) is (disposition == "CLOSE")
    if reconciled:
        await auto.scan_pending()
        assert ("OUTBOUND_REPORT", host.plate_id) not in work
        assert ("OUTBOUND_PLATE", host.plate_id) in work
    async with host.factory.begin() as session:
        for now in (100, 110):
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=host.plate_id, now_ms=now
            )
        assert not plate.pending
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.trigger_plate_id == plate.id,
                )
            )
            == 1
        )
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=120
        )
        assert await host.outbound.bin_closed(session, work)


async def test_closed_rejected_plate_does_not_hold_target_departure(decided_outbound):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="REJECT",
            business_exception_code="MATERIAL_REJECTED",
            source_disposition="CLOSE",
            detailMessage="OK",
            ng_locator=dict(type="NG_ZONE", zone_code="MATERIAL_NG_01"),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = plate.put_command_code
        target_job = next(
            job
            for job in await host.dispatch.jobs(
                session, work_line_id=1, source_kinds=("AUTO_OUTBOUND_TARGET",)
            )
            if job.origin.task_id == "t"
        )
        assert put_code

    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=80)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=90)
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=100)
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=110)
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=120)
        work = await session.get(AutoBinWork, host.work_id)
        assert (
            await flow.release_bin(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=120)
            == "RELEASING"
        )
        release_code = work.release_command_code
    assert (await host.result(release_code)).status_code == 200
    async with host.factory.begin() as session:
        await flow.consume_release(session, work_line_id=1, arrival_id=work.arrival_id, now_ms=121)
        await racks.advance(session, task_id="t", now_ms=122)
        target = await host.dispatch.progress(session, job_id=target_job.id)
        assert [action.kind for action in target.actions] == ["ENTER", "DEPARTURE_DECIDE"]

    await host.settle(
        "rack.departure_decide@v1",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "SMT_B"}},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=123)
        await racks.advance(session, task_id="t", now_ms=124)
        target = await host.dispatch.progress(session, job_id=target_job.id)
        assert [action.kind for action in target.actions] == [
            "ENTER",
            "DEPARTURE_DECIDE",
            "DEPART",
        ]


async def test_return_faces_reuse_original_job_only_after_terminal_face(outbound_host):
    from app.businesses.smt_auto.models import AutoOutboundSourceMember
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = outbound_host
    racks = OutboundRacks(host.outbound, station_id="station")
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        members = [
            AutoOutboundSourceMember(
                task_id="t",
                plan_input_id=plan.id,
                plan_revision=1,
                rack_id="return",
                rack_face=face,
                rack_array_index=0,
            )
            for face in ("B", "A")
        ]
        session.add_all(members)
        await session.flush()
        await racks.advance(session, task_id="t", now_ms=20)
        a, b = members[1], members[0]
        assert a.rack_job_id and b.rack_job_id is None
        ids = a.id, b.id
        job = await host.dispatch.progress(session, job_id=a.rack_job_id)
        await apply_outcome(
            session, job.actions[-1], "SUCCEEDED", point="Q", rack_id="return", arrival_face="A"
        )
        await racks.advance(session, task_id="t", now_ms=30)
    await host.settle("outbound.return_rack.work_plan@v1", dict(result="NO_WORK"))
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=40)
        await racks.advance(session, task_id="t", now_ms=50)
        a, b = [await session.get(AutoOutboundSourceMember, identity) for identity in ids]
        assert a.rack_job_id == b.rack_job_id
        assert b.preparation_action_id
        job = await host.dispatch.progress(session, job_id=b.rack_job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "ROTATE"]
        assert job.actions[-1].target_face == "B"


async def test_empty_wire_keeps_bin_unbound_and_rack_original_revision(outbound_host):
    from pydantic import ValidationError

    from app.businesses.smt_auto.request.outbound_source_empty_decide_v1 import EmptyDecide
    from app.integrations.model_payload import validated_model_payload

    bin_data = dict(
        task_id="t",
        source_locator=dict(type="BIN_CELL", bin_code="B1", cell_id="C1"),
        observed_at=99,
    )
    rack_data = dict(
        task_id="t",
        source_locator=dict(type="RACK_SLOT", rack_id="R1", rack_face="A", slot_id="S1"),
        observed_at=99,
        plan_revision=1,
    )
    for data in (bin_data, rack_data):
        assert validated_model_payload(EmptyDecide.model_validate(data)) == data
    for data in (
        {**bin_data, "plan_revision": 1},
        {**bin_data, "plan_revision": None},
        {k: v for k, v in rack_data.items() if k != "plan_revision"},
    ):
        with pytest.raises(ValidationError):
            EmptyDecide.model_validate(data)


async def test_target_replacement_waits_for_applied_movement_report(decided_outbound):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    await host.settle("outbound.bin.material.decide@v1", accepted_target())
    async with host.factory.begin() as session:
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        put_code = first.put_command_code
        job_id = first.target_rack_job_id
        assert put_code
    await host.settle("outbound.bin.work_plan@v1", ready_bin() | {"reel_thickness": 20})
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=65)
        second = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.id != host.plate_id)
        )
        second_id = second.id
        await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=70)
        assert second.pick_command_code is None
        assert not any(
            action.kind == "DEPART"
            for action in (await host.dispatch.progress(session, job_id=job_id)).actions
        )
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        second = await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=75)
        pick_code = second.pick_command_code
        assert pick_code
    await host.arm_result(pick_code, scan=True)
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=80)
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_preparation="REPLACE",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="replacement",
                rack_face="A",
                slot_id="A2",
                rack_layer=2,
                rack_column=2,
            ),
        ),
    )
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=second_id, now_ms=90)
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=110
        )
        assert first.report_message_id and first.report_consumed_at_ms is None
        demand, handoff = await racks.ports.facts(session, job_id=job_id)
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"
        command = await session.scalar(
            select(IntegrationExchange).where(IntegrationExchange.command_code == put_code)
        )
        # Simulate an outstanding original PUT at the point of the replacement check.
        command.status = "WAITING_RESULT"
        await session.flush()
        demand, handoff = await racks.ports.facts(session, job_id=job_id)
        assert (demand.state, handoff.state) == ("CONTINUE", "BLOCKED")
        await racks.advance(session, task_id="t", now_ms=111)
        assert [
            action.kind for action in (await host.dispatch.progress(session, job_id=job_id)).actions
        ] == ["ENTER"]
        command.status = "SUCCEEDED"
        await session.flush()
        final = await session.get(AutoOutboundDecisionRound, first.final_decision_round_id)
        final.unresolved = True
        await session.flush()
        assert (await racks.ports.facts(session, job_id=job_id))[0].state == "CONTINUE"
        final.unresolved = False
        await session.flush()
        await racks.advance(session, task_id="t", now_ms=120)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER"]
        second = await session.get(AutoOutboundPlate, second_id)
        assert second.target_rack_job_id is None
        assert second.put_command_code is None
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=130)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER"]
        await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=131)
        await racks.advance(session, task_id="t", now_ms=132)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "DEPARTURE_DECIDE"]
    await host.settle(
        "rack.departure_decide@v1",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "T_OUT"}},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=140)
        await racks.advance(session, task_id="t", now_ms=141)
        job = await host.dispatch.progress(session, job_id=job_id)
        assert [action.kind for action in job.actions] == ["ENTER", "DEPARTURE_DECIDE", "DEPART"]


@pytest.mark.parametrize(
    "outbound_host", [("ARM04", "ARM03", None, ("610002", "OUT63", "90"))], indirect=True
)
@pytest.mark.parametrize("departure_position", ["OUT63", "SMT_B"])
async def test_same_transfer_rack_reenters_after_departure_without_replace(
    decided_outbound, departure_position
):
    from app.businesses.smt_auto.outbound_racks import OutboundRacks
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = decided_outbound
    racks = OutboundRacks(host.outbound, station_id="station")
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    locator = dict(
        type="RACK_SLOT",
        rack_id="610002",
        rack_face="90",
        slot_id="A1",
        rack_layer=1,
        rack_column=1,
    )
    operation = "outbound.bin.material.decide@v1"
    await host.settle(operation, {"result": "ACCEPT", "target_locator": locator})
    async with host.factory.begin() as session:
        first = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        old_job_id, put_code = first.target_rack_job_id, first.put_command_code
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=61)
        old_bin = await session.get(AutoBinWork, host.work_id)
        assert (
            await flow.release_bin(
                session, work_line_id=1, arrival_id=old_bin.arrival_id, now_ms=61
            )
            == "RELEASING"
        )
        release_code = old_bin.release_command_code
    assert (await host.result(release_code)).status_code == 200
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=70)
        await flow.consume_release(
            session, work_line_id=1, arrival_id=old_bin.arrival_id, now_ms=71
        )
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        await host.outbound.advance_report(session, plate_id=host.plate_id, now_ms=72)
        await racks.advance(session, task_id="t", now_ms=73)
    await host.settle(
        "rack.departure_decide@v1",
        {"result": "READY", "rack_destination": {"type": "ZONE", "location_code": "SMT_B"}},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=74)
        await racks.advance(session, task_id="t", now_ms=75)
        old_job = await host.dispatch.progress(session, job_id=old_job_id)
        depart = old_job.actions[-1]
        assert depart.kind == "DEPART"
        await respond(
            session,
            depart.submit_operation_id,
            202,
            "RECEIVED",
            {"transport_task_id": depart.transport_task_id},
        )
        await racks.advance(session, task_id="t", now_ms=76)
        assert (await host.dispatch.progress(session, job_id=old_job_id)).state == "COMPLETED"
    work = await host.scan(timestamp=90, bin_code="A000000002-C")
    async with host.factory.begin() as session:
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=91
        )
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C2",
            cell_index=2,
            bin_type=3,
            reel_layer=1,
            reel_thickness=15.3,
            reel_diameter=7,
        ),
    )
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=92
        )
        plate_id = await session.scalar(
            select(AutoOutboundPlate.id).where(AutoOutboundPlate.bin_work_id == work.id)
        )
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=93)
        pick_code = plate.pick_command_code
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=94)
    await host.settle(
        operation,
        {
            "result": "ACCEPT",
            "detailMessage": "OK",
            "target_locator": {**locator, "slot_id": "A2", "label": "A2"},
        },
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=95)
        await racks.advance(session, task_id="t", now_ms=96)
        assert plate.target_rack_job_id is None and plate.put_command_code is None

        await apply_outcome(
            session, depart, "CANCELLED", point=departure_position, rack_id="610002"
        )
        await racks.advance(session, task_id="t", now_ms=97)
        await racks.advance(session, task_id="t", now_ms=98)
        jobs = await host.dispatch.jobs(
            session, work_line_id=1, source_kinds=("AUTO_OUTBOUND_TARGET",)
        )
        if departure_position == "OUT63":
            assert len(jobs) == 1
            assert plate.target_rack_job_id is None and plate.put_command_code is None
            return
        assert len(jobs) == 2
        returned = next(job for job in jobs if job.id != old_job_id)
        assert returned.origin.rack_id == "610002"
        (enter,) = returned.actions
        assert (enter.kind, enter.target, enter.target_face) == (
            "ENTER",
            {"kind": "RACK_POSITION", "location_code": "OUT63"},
            "90",
        )
        assert enter.submit_operation_id != depart.submit_operation_id
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=99)
        assert plate.put_command_code is None
        await apply_outcome(
            session, enter, "SUCCEEDED", point="OUT63", rack_id="610002", arrival_face="90"
        )
        await racks.advance(session, task_id="t", now_ms=100)
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=101)
        assert plate.put_command_code and plate.target_preparation_action_id == enter.id
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.command_code == plate.put_command_code
            )
        )
        assert command.device_code == "ARM04"


async def test_final_decision_put_and_work_plan_roll_back_together(decided_outbound):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    rounds = (
        select(func.count())
        .select_from(AutoOutboundDecisionRound)
        .where(AutoOutboundDecisionRound.operation == "outbound.bin.work_plan@v1")
    )
    async with host.factory() as session:
        await session.begin()
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        assert plate.put_command_code
        assert await session.scalar(rounds) == 2
        await session.rollback()
    async with host.factory.begin() as session:
        plate = await session.get(AutoOutboundPlate, host.plate_id)
        assert plate.put_command_code is None
        assert plate.final_decision_round_id is None
        assert await session.scalar(rounds) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 1
        )
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=70)
        assert plate.put_command_code
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=71)
        assert await session.scalar(rounds) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )


async def test_transfer_demand_requires_complete_original_plan_coverage(decided_outbound):
    host = decided_outbound
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        assert (await ports.facts(session, job_id=plan.target_job_id))[0].state == "CONTINUE"
        plan.effective_request = {
            **plan.effective_request,
            "added_return_racks": [{"rack_id": "missing", "rack_faces": ["A"]}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plan.target_job_id))[0].state == "UNKNOWN"


@pytest.mark.parametrize("tail", ["unknown", "WAIT", "READY"])
async def test_replacement_does_not_wait_for_its_own_successor(decided_outbound, tail):
    host = decided_outbound
    await host.settle(
        "outbound.bin.material.decide@v1",
        accepted_target(rack_id="replacement", preparation="REPLACE"),
    )
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=host.plate_id, now_ms=60)
    if tail != "unknown":
        result = ready_bin() if tail == "READY" else {"result": "WAIT", "retry_after_ms": 10}
        await host.settle("outbound.bin.work_plan@v1", result)
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=61)
        successor = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.id != host.plate_id)
        )
        assert (successor is not None) is (tail == "READY")
        if successor is not None:
            assert successor.pick_command_code is None
        plan = await session.scalar(select(AutoTaskInput))
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plan.target_job_id)
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")


async def test_closed_empty_pick_does_not_hold_later_target_replacement(outbound_host):
    host = outbound_host
    await host.settle("outbound.bin.work_plan@v1", ready_bin("C1", 1))
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=30)
        first = await session.scalar(select(AutoOutboundPlate))
        first_id = first.id
        await host.outbound.advance(session, work_line_id=1, plate_id=first_id, now_ms=40)
        first_pick = first.pick_command_code

    async def advance(plate_id, now_ms):
        async with host.factory.begin() as session:
            return await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=now_ms
            )

    assert (
        await host.arm_result(first_pick, status="FAILED", empty="PICK_FAILED")
    ).status_code == 200
    await advance(first_id, 50)
    await host.settle("outbound.source.empty_decide@v1", {"result": "SOURCE_DONE"})
    await advance(first_id, 60)
    await host.settle("outbound.bin.work_plan@v1", ready_bin())
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=70)
        first = await session.get(AutoOutboundPlate, first_id)
        assert not first.pending and await host.outbound.closed(session, first)
        current = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.id != first_id)
        )
        current_id = current.id
    current = await advance(current_id, 80)
    assert (await host.arm_result(current.pick_command_code, scan=True)).status_code == 200
    await advance(current_id, 90)
    await host.settle(
        "outbound.bin.material.decide@v1",
        accepted_target(rack_id="replacement", preparation="REPLACE"),
    )
    await advance(current_id, 100)
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        demand, handoff = await AutoRackPorts(host.dispatch, outbound=host.outbound).facts(
            session, job_id=plan.target_job_id
        )
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")


@pytest.mark.parametrize("ready", [None, False, True])
async def test_other_source_prefetch_cannot_hold_target_replacement(decided_outbound, ready):
    host = decided_outbound
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        job_id = plan.target_job_id
        plan.effective_request = {
            **plan.effective_request,
            "added_return_racks": [{"rack_id": "return", "rack_faces": ["A"]}],
        }
    racks, _, rack_plate_id = await return_face(host, ready=ready)
    await host.settle(
        "outbound.bin.material.decide@v1",
        accepted_target(rack_id="replacement", preparation="REPLACE"),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=201
        )
        assert plate.put_command_code is None
        if rack_plate_id is not None:
            rack_plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=rack_plate_id, now_ms=202
            )
            assert rack_plate.pick_command_code is None
        demand, handoff = await racks.ports.facts(session, job_id=job_id)
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")


@pytest.mark.parametrize("outbound_host", [("ARM02", "ARM01", "NO_BIN")], indirect=True)
async def test_target_only_waits_for_production_sources(outbound_host):
    from app.businesses.smt_auto.task_flow import TaskFlow

    host = outbound_host
    flow = TaskFlow(
        host.requests, roller=host.roller, dispatch=host.dispatch, outbound=host.outbound
    )
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        job_id = plan.target_job_id
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        assert (await ports.facts(session, job_id=job_id))[0].state == "CONTINUE"
        for now in (20, 30):
            assert await flow.advance(session, task_id="t", now_ms=now) == "WAITING"
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        assert not await session.scalar(
            select(AutoTaskRequest).where(AutoTaskRequest.purpose == "COMPLETION")
        )
        assert (await ports.facts(session, job_id=job_id))[0].state == "CONTINUE"


@pytest.mark.parametrize("outbound_host", [("ARM02", "ARM01", "NO_BIN")], indirect=True)
async def test_unstarted_withdrawn_supply_can_confirm_without_production(outbound_host):
    from app.businesses._components.sorting_task.contracts import Cancel, PlanDelta
    from app.businesses.smt_auto.task_flow import TaskFlow
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    host = outbound_host
    service = AutoTaskService(roller=host.roller)
    delta = PlanDelta(
        task_id="t",
        plan_revision=2,
        added_bin_source_racks=[{"rack_id": "unused", "rack_face": ["A"]}],
    )
    cancel = Cancel(
        task_id="t",
        cancel_scope="PLAN_MEMBERS",
        bin_source_racks=[{"plan_revision": 2, "rack_id": "unused", "rack_face": ["A"]}],
    )
    async with host.factory.begin() as session:
        for name, payload, accept in (
            ("plan_delta", delta, service.accept_task_plan_delta),
            ("cancel", cancel, service.accept_task_cancel),
        ):
            await accept(
                CallbackContext(
                    session,
                    1,
                    f"supply-{name}",
                    50,
                    payload.model_dump(mode="json", exclude_none=True),
                    operation=f"sorting.task.{name}@v1",
                ),
                payload,
            )
        flow = TaskFlow(host.requests, roller=host.roller, dispatch=host.dispatch)
        assert await flow.advance(session, task_id="t", now_ms=100) == "CONFIRMING"


async def test_mixed_withdrawn_sources_release_target_before_report(decided_outbound):
    from app.businesses._components.sorting_task.contracts import Cancel, PlanDelta
    from app.businesses.smt_auto.task_service import AutoTaskService
    from app.integrations.wms_callback import CallbackContext

    host = decided_outbound
    service = AutoTaskService(roller=host.roller)
    async with host.factory.begin() as session:
        delta = PlanDelta(
            task_id="t",
            plan_revision=2,
            added_return_racks=[{"rack_id": "unused-return", "rack_faces": ["A"]}],
            added_bin_source_racks=[{"rack_id": "unused-supply", "rack_face": ["A"]}],
        )
        await service.accept_task_plan_delta(
            CallbackContext(
                session,
                1,
                "mixed-plan",
                51,
                delta.model_dump(mode="json", exclude_none=True),
                operation="sorting.task.plan_delta@v1",
            ),
            delta,
        )
        cancel = Cancel(
            task_id="t",
            cancel_scope="PLAN_MEMBERS",
            direct_pick_sources=[
                {"plan_revision": 2, "rack_id": "unused-return", "rack_face": "A"}
            ],
            bin_source_racks=[{"plan_revision": 2, "rack_id": "unused-supply", "rack_face": ["A"]}],
        )
        await service.accept_task_cancel(
            CallbackContext(
                session,
                1,
                "mixed-cancel",
                52,
                cancel.model_dump(mode="json", exclude_none=True),
                operation="sorting.task.cancel@v1",
            ),
            cancel,
        )
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="A1",
                rack_layer=1,
                rack_column=1,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=60
        )
        code = plate.put_command_code
        assert code
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    await host.arm_result(code)
    async with host.factory.begin() as session:
        await consume_bin_plan(host, session, now_ms=69)
        plate = await host.outbound.advance(
            session, work_line_id=1, plate_id=host.plate_id, now_ms=70
        )
        assert plate.report_message_id and plate.report_consumed_at_ms is None
        assert (await session.get(AutoTask, "t")).status == "EXECUTING"
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plate.target_rack_job_id)
        assert (demand.state, handoff.state) == ("NONE", "CLEAR")
        cancellation = await session.scalar(
            select(AutoTaskInput).where(AutoTaskInput.operation_id == "mixed-cancel")
        )
        original_cancellation = cancellation.effective_request
        cancellation.effective_request = {
            **cancellation.effective_request,
            "direct_pick_sources": [{"plan_revision": 2, "rack_id": "wrong", "rack_face": "A"}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plate.target_rack_job_id))[0].state == "CONTINUE"
        cancellation.effective_request = {
            **original_cancellation,
            "bin_source_racks": [{"plan_revision": 2, "rack_id": "wrong", "rack_face": ["A"]}],
        }
        await session.flush()
        assert (await ports.facts(session, job_id=plate.target_rack_job_id))[0].state == "CONTINUE"


@pytest.mark.parametrize("scanned", [False, True])
async def test_original_pick_failed_wait_retry_and_source_done(outbound_host, scanned):
    from app.businesses.smt_auto.models import AutoOutboundDecisionRound

    host = outbound_host
    ready = dict(
        result="READY",
        cell_id="C1",
        cell_index=1,
        bin_type=3,
        reel_layer=2,
        reel_thickness=15.3,
        reel_diameter=7,
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        arrival_id = work.arrival_id
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=20)
    await host.settle("outbound.bin.work_plan@v1", ready)
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=30)
        plate = await session.scalar(select(AutoOutboundPlate))
        first_id = plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=first_id, now_ms=40)
        first_code = plate.pick_command_code

    async def advance(plate_id, now_ms):
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=now_ms)

    await advance(first_id, 100)
    async with host.factory() as session:
        assert (
            await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE"
                )
            )
            is None
        )
    if scanned:
        assert (await host.platform_scan(first_code)).status_code == 200
    assert (
        await host.arm_result(first_code, status="FAILED", empty="PICK_FAILED")
    ).status_code == 200
    await advance(first_id, 110)
    if scanned:
        async with host.factory() as session:
            assert not await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose.in_(("EMPTY_DECIDE", "MATERIAL_DECIDE"))
                )
            )
        return
    await host.settle("outbound.source.empty_decide@v1", dict(result="WAIT", retry_after_ms=20))
    await advance(first_id, 120)
    await advance(first_id, 139)
    async with host.factory() as session:
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE",
                )
            )
        )
        assert len(rounds) == 1
    await advance(first_id, 140)
    await host.settle("outbound.source.empty_decide@v1", dict(result="RETRY"))
    await advance(first_id, 150)
    await advance(first_id, 151)
    async with host.factory.begin() as session:
        first = await session.get(AutoOutboundPlate, first_id)
        rounds = list(
            await session.scalars(
                select(AutoOutboundDecisionRound)
                .where(
                    AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE",
                )
                .order_by(AutoOutboundDecisionRound.round_no)
            )
        )
        assert [r.request_data for r in rounds] == [
            dict(task_id=first.task_id, source_locator=first.source_locator, observed_at=99)
        ] * 2
        assert rounds[0].operation_id != rounds[1].operation_id
        assert rounds[1].previous_round_id == rounds[0].id
        retry = await session.scalar(
            select(AutoOutboundPlate).where(AutoOutboundPlate.grant_round_id == rounds[1].id)
        )
        assert retry.source_locator == first.source_locator
        assert retry.source_params == first.source_params
        retry_id = retry.id
        await host.outbound.advance(session, work_line_id=1, plate_id=retry_id, now_ms=160)
        assert retry.pick_command_code and retry.pick_command_code != first_code
        retry_code = retry.pick_command_code
    assert (
        await host.arm_result(retry_code, status="FAILED", empty="PICK_FAILED")
    ).status_code == 200
    await advance(retry_id, 170)
    await host.settle("outbound.source.empty_decide@v1", dict(result="SOURCE_DONE"))
    await advance(retry_id, 180)
    await advance(retry_id, 181)
    async with host.factory() as session:
        work = await session.get(AutoBinWork, host.work_id)
        assert not await host.outbound.bin_closed(session, work)
    await host.settle("outbound.bin.work_plan@v1", dict(result="WAIT", retry_after_ms=1))
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=190
        )
        await host.work_plans.start_bin(session, work_line_id=1, arrival_id=arrival_id, now_ms=191)
    await host.settle("outbound.bin.work_plan@v1", dict(result="NO_WORK"))
    async with host.factory.begin() as session:
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=arrival_id, now_ms=200
        )
        work = await session.get(AutoBinWork, host.work_id)
        assert await host.outbound.bin_closed(session, work)
        plates = list(await session.scalars(select(AutoOutboundPlate)))
        assert len(plates) == 2
        assert all(not p.pending and p.report_message_id is None for p in plates)


@pytest.mark.parametrize(
    "stage,next_slot,conflict",
    [
        ("PUT_FAILED", None, False),
        ("PICK_FAILED", "S1", False),
        ("PICK_FAILED", "S2", False),
        ("PICK_FAILED", "S2", True),
    ],
)
async def test_return_slot_empty_only_for_original_pick_failure(
    outbound_host, stage, next_slot, conflict
):
    from app.businesses.smt_auto.models import AutoOutboundDecisionRound, AutoOutboundSourceMember
    from app.businesses.smt_auto.outbound_racks import OutboundRacks

    host = outbound_host
    racks = OutboundRacks(host.outbound, station_id="station")
    async with host.factory.begin() as session:
        plan = await session.scalar(select(AutoTaskInput))
        member = AutoOutboundSourceMember(
            task_id="t",
            plan_input_id=plan.id,
            plan_revision=1,
            rack_id="return",
            rack_face="A",
            rack_array_index=0,
        )
        session.add(member)
        await session.flush()
        member_id = member.id
        await racks.advance(session, task_id="t", now_ms=20)
        progress = await host.dispatch.progress(session, job_id=member.rack_job_id)
        await apply_outcome(
            session,
            progress.actions[-1],
            "SUCCEEDED",
            point="Q",
            rack_id="return",
            arrival_face="A",
        )
        await racks.advance(session, task_id="t", now_ms=30)
    ready = dict(
        result="READY",
        rack_id="return",
        rack_face="A",
        slot_id="S1",
        rack_layer=2,
        rack_column=3,
        reel_thickness=15.3,
        reel_diameter=7,
    )
    await host.settle("outbound.return_rack.work_plan@v1", ready)
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=40)
        plate = await session.scalar(select(AutoOutboundPlate))
        plate_id = plate.id
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        code = plate.pick_command_code
    result = await host.arm_result(code, status="FAILED", empty=stage)
    assert result.status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound).where(
                AutoOutboundDecisionRound.purpose == "EMPTY_DECIDE"
            )
        )
        if stage != "PICK_FAILED":
            assert round_ is None
            return
        assert round_.request_data == dict(
            task_id="t",
            source_locator=dict(type="RACK_SLOT", rack_id="return", rack_face="A", slot_id="S1"),
            plan_revision=1,
            observed_at=99,
        )
    await host.settle("outbound.source.empty_decide@v1", dict(result="SOURCE_DONE"))
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert not await host.outbound.face_source_terminal(session, member)
    await host.settle("outbound.return_rack.work_plan@v1", dict(result="WAIT", retry_after_ms=1))
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=80)
        await racks.advance(session, task_id="t", now_ms=81)
    if conflict:
        changed = await host.arm_result(code, status="FAILED", empty="PUT_FAILED")
        assert changed.status_code == 409
    await host.settle(
        "outbound.return_rack.work_plan@v1",
        {**ready, "slot_id": next_slot, "rack_column": 4 if next_slot == "S2" else 3},
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=90)
        plates = list(
            await session.scalars(select(AutoOutboundPlate).order_by(AutoOutboundPlate.id))
        )
        round_ = await session.scalar(
            select(AutoOutboundDecisionRound)
            .where(
                AutoOutboundDecisionRound.operation == "outbound.return_rack.work_plan@v1",
            )
            .order_by(AutoOutboundDecisionRound.round_no.desc())
        )
        assert round_.request_data == dict(
            task_id="t",
            rack_id="return",
            rack_face="A",
            arrived_at=30,
        )
        if next_slot == "S1":
            assert [p.id for p in plates] == [plate_id]
            assert round_.unresolved
        else:
            original, successor = plates
            assert original.id == plate_id
            assert successor.source_locator["slot_id"] == next_slot
            assert successor.source_params["rack_column"] == 4
            await host.outbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=91)
            if conflict:
                assert successor.pick_command_code is None
            else:
                assert successor.pick_command_code and successor.pick_command_code != code
