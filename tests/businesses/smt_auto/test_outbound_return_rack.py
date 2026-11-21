# ruff: noqa: F405
from tests.businesses.smt_auto._outbound_support import *  # noqa: F401,F403


async def test_applied_pick_reconciliation_remains_readable_for_material_flow():
    from app.businesses.smt_auto.outbound import Outbound

    result = {
        "command_code": "pick",
        "device_code": "ARM03",
        "result": "SUCCESS",
        "finish_time": 1,
        "data": {"actual_qty": 1, "pick_and_put_result": "PUT_FINISHED"},
    }

    class Ecs:
        async def get_by_call(self, session, *, owner, call_key):
            return SimpleNamespace(
                command_code="pick",
                status="SUCCEEDED",
                device_code="ARM03",
                task_type="PICK_AND_PUT",
                hold_reason=None,
                needs_reconciliation=True,
                result=result,
            )

    outbound = Outbound.__new__(Outbound)
    outbound.ecs = Ecs()
    assert await outbound._arm_result(
        None, key="pick-key", code="pick", device_code="ARM03", phase="pick"
    )
    assert (
        await outbound._arm_result(
            None, key="pick-key", code="pick", device_code="ARM03", phase="put"
        )
        is None
    )


@pytest.mark.parametrize("runtime_authority", [False, True])
@pytest.mark.parametrize("correction", ["recorded", "missing", "different_slot", "valid_old"])
async def test_return_face_pick_requires_recorded_correction_of_invalid_ready(
    outbound_host, monkeypatch, runtime_authority, correction
):
    from app.core.config import get_settings

    host = outbound_host
    operation = "outbound.return_rack.work_plan@v1"
    ready = dict(
        result="READY",
        rack_id="return",
        rack_face="A",
        slot_id="S1",
        rack_layer=2,
        rack_column=3,
        reel_thickness=20,
        reel_diameter=330,
    )
    # Retained original evidence and the operational projection are both populated
    # on an ordinary response. Switch readers after saving these original facts.
    original_reply = (
        ready
        if correction == "valid_old"
        else {**ready, "reel_thickness": None, "reel_diameter": None}
    )
    racks, member_id, _ = await return_face(host, ready=original_reply, consume=False)
    if runtime_authority:
        from app.models import EcsCommand, EcsResultEvidence, IntegrationReference

        async with host.factory.kw["bind"].begin() as connection:
            for model in (IntegrationReference, IntegrationRecord, IntegrationExchange):
                await connection.run_sync(model.__table__.drop)
            for model in (EcsCommand, EcsResultEvidence):
                await connection.run_sync(model.__table__.create)
    monkeypatch.setattr(get_settings(), "INTEGRATION_RUNTIME_AUTHORITY", runtime_authority)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.businesses.smt_auto.service import SmtAutoService
    from app.businesses.smt_auto.transactions import AutoS1Transactions
    from scripts.correct_smt_return_work_plan import correct

    service = SmtAutoService.__new__(SmtAutoService)
    service.dispatch, service.roller, service.wms = host.dispatch, host.roller, host.requests
    service.inbound = host.outbound
    service.work_plans = host.outbound.work_plans
    factory = async_sessionmaker(host.factory.kw["bind"])
    service.transactions = AutoS1Transactions(
        SimpleNamespace(session_factory=factory), service=service
    )
    monkeypatch.setattr(
        "app.tasks.business_tasks._runtime",
        lambda factory, business: SimpleNamespace(services={"smt_auto": service}),
    )

    async with host.factory.begin() as session:
        member = await session.get(AutoOutboundSourceMember, member_id)
        old = await session.get(AutoOutboundDecisionRound, member.first_work_plan_round_id)
        original_id = old.operation_id
        if correction == "missing":
            await racks.advance(session, task_id="t", now_ms=40)

    replacement = None if correction == "missing" else await correct(factory, original_id)
    if correction in ("missing", "valid_old"):
        assert replacement is None
    else:
        assert replacement and replacement != original_id
        assert await correct(factory, original_id) == replacement

    async with host.factory() as session:
        member = await session.get(AutoOutboundSourceMember, member_id)
        old = await session.get(AutoOutboundDecisionRound, member.first_work_plan_round_id)
        if correction in ("missing", "valid_old"):
            assert old.unresolved
        else:
            successor = await session.scalar(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.previous_round_id == old.id
                )
            )
            assert successor.operation_id == replacement
            assert not old.unresolved and old.consumed_at_ms is None
    if correction in ("missing", "valid_old"):
        return

    await host.settle(
        operation, {**ready, "slot_id": "S2"} if correction == "different_slot" else ready
    )
    async with host.factory.begin() as session:
        await racks.advance(session, task_id="t", now_ms=50)
        plate_id = await session.scalar(select(AutoOutboundPlate.id))
        if correction == "different_slot":
            assert plate_id is None
            return
        assert plate_id is not None
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
        assert plate.pick_command_code is not None
        command_code = plate.pick_command_code
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        assert plate.pick_command_code == command_code


@pytest.mark.parametrize("event_first", [False, True])
async def test_return_rack_pick_uses_independent_platform_scan(outbound_host, event_first):
    host = outbound_host
    _, _, plate_id = await return_face(host)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        pick_code = plate.pick_command_code

    if event_first:
        assert (await host.platform_scan(pick_code)).status_code == 200
    else:
        assert (await host.arm_result(pick_code, barcode=None, scanned_at=None)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=100)
        assert (
            await session.scalar(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.plate_id == plate_id
                )
            )
            is None
        )

    if event_first:
        assert (await host.arm_result(pick_code)).status_code == 200
    else:
        event = await host.platform_scan(pick_code)
        assert event.status_code == 200
        assert not event.diagnostic
    assert (await host.platform_scan(pick_code)).outcome == "duplicate"
    async with host.factory.begin() as session:
        for now_ms in (110, 120):
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=now_ms
            )
        assert plate.pick_command_code == pick_code
        assert plate.scanned_at_ms == 88
        (decision,) = tuple(
            await session.scalars(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.plate_id == plate_id
                )
            )
        )
        assert decision.operation == "outbound.rackslot.material.decide@v1"
        assert decision.request_data["slot_id"] == "S1"
        assert decision.request_data["barcode"] == "原始料盘," * 60
        assert decision.request_data["scanned_at"] == 88
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 1
        )


@pytest.mark.parametrize("event_first", [False, True])
async def test_return_rack_scan_conflict_waits_for_linked_correction(outbound_host, event_first):
    host = outbound_host
    _, _, plate_id = await return_face(host)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        code = plate.pick_command_code

    async def result():
        assert (await host.arm_result(code, barcode="result", scanned_at=88)).status_code == 200

    async def scan():
        assert (await host.platform_scan(code, barcode="event")).status_code == 200

    if event_first:
        await scan()
        await result()
    else:
        await result()
        await scan()
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=100)
        rounds = select(AutoOutboundDecisionRound).where(
            AutoOutboundDecisionRound.plate_id == plate_id
        )
        assert await session.scalar(rounds) is None

    assert (
        await host.platform_scan(code, barcode="confirmed", timestamp=101, scan_revision=2)
    ).status_code == 200
    async with host.factory.begin() as session:
        for now in (110, 120):
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=now)
        (decision,) = (await session.scalars(rounds)).all()
        assert decision.operation == "outbound.rackslot.material.decide@v1"
        assert decision.request_data["barcode"] == "confirmed"
        assert decision.request_data["scanned_at"] == 101


@pytest.mark.parametrize("next_mode", ["NO_WORK", "valid", "wrong_face", "repeat_slot", "ng"])
async def test_return_face_original_pick_and_final_target(outbound_host, next_mode):
    host = outbound_host
    racks, member_id, plate_id = await return_face(host)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        assert plate.pick_command_code
        pick_code = plate.pick_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == pick_code,
            )
        )
        raw = json.loads(await read_history_body(session, (command).request_record_id))
        assert raw["params"]["source"]["location_id"] == "STATION_B"
        assert raw["params"]["source"]["rack_layer"] == "2"
        assert raw["params"]["source"]["reel_diameter"] == 13
        assert plate.source_params["reel_diameter"] == 330.2
    assert (await host.arm_result(pick_code, scan=True)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
    if next_mode == "ng":
        ng = dict(type="NG_ZONE", zone_code="MATERIAL_NG_01")
        await host.settle(
            "outbound.rackslot.material.decide@v1",
            dict(
                result="REJECT",
                source_disposition="CLOSE",
                ng_locator=ng,
            ),
        )
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=70
            )
            put_code = plate.put_command_code
            assert put_code
            command = await session.scalar(
                select(IntegrationExchange).where(
                    IntegrationExchange.role == "ECS_COMMAND",
                    IntegrationExchange.command_code == put_code,
                )
            )
            line = await session.get(WorkLine, 1)
            assert command.device_code == line.device_bindings["BIN_ARM"]
            assert json.loads(await read_history_body(session, (command).request_record_id))[
                "params"
            ] == dict(
                source=dict(location_id="STATION_SCAN", location_type="SCAN_PLATFORM"),
                target=dict(location_id="STATION_NG", location_type="SCAN_NG"),
            )
        assert (await host.arm_result(put_code)).status_code == 200
        async with host.factory.begin() as session:
            plate = await host.outbound.advance(
                session, work_line_id=1, plate_id=plate_id, now_ms=80
            )
            assert plate.report_data["to_locator"] == ng
            assert plate.report_data["occurred_at"] == 99
            assert not await host.outbound.closed(session, plate)
        await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
        async with host.factory.begin() as session:
            plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=90)
            assert await host.outbound.closed(session, plate)
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=100)
            assert not plate.pending
            member = await session.get(AutoOutboundSourceMember, member_id)
            assert not await host.outbound.face_closed(session, member)
        await host.settle("outbound.return_rack.work_plan@v1", {"result": "NO_WORK"})
        async with host.factory.begin() as session:
            await racks.advance(session, task_id="t", now_ms=110)
            member = await session.get(AutoOutboundSourceMember, member_id)
            assert await host.outbound.face_closed(session, member)
        return
    decision = dict(
        result="ACCEPT",
        target_locator=dict(
            type="RACK_SLOT",
            rack_id="target",
            rack_face="A",
            slot_id="T2",
            rack_layer=4,
            rack_column=5,
        ),
    )
    await host.settle("outbound.rackslot.material.decide@v1", decision)
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=70)
        assert plate.put_command_code
        put_code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == put_code,
            )
        )
        target = json.loads(await read_history_body(session, (command).request_record_id))[
            "params"
        ]["target"]
        assert target["reel_thickness"] == 15.3
        assert target["reel_diameter"] == 13
        assert target["rack_layer"] == "4"
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert not await host.outbound.face_source_terminal(session, member)
        demand, handoff = await racks.ports.facts(session, job_id=member.rack_job_id)
        assert demand.state == "CONTINUE"
        assert handoff.state == "CLEAR"
    if next_mode == "NO_WORK":
        next_result = {"result": "NO_WORK"}
    else:
        next_result = dict(
            result="READY",
            rack_id="return",
            rack_face="B" if next_mode == "wrong_face" else "A",
            slot_id="S1" if next_mode == "repeat_slot" else "S2",
            rack_layer=1,
            rack_column=6,
            reel_thickness=22,
            reel_diameter=13,
        )
    await host.settle("outbound.return_rack.work_plan@v1", next_result)
    if next_mode == "valid":
        await host.arm_ack(put_code)
    async with host.factory.begin() as session:
        await host.outbound.work_plans.consume_return_face(
            session, work_line_id=1, member_id=member_id, now_ms=71
        )
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert await host.outbound.face_source_terminal(session, member) is (next_mode == "NO_WORK")
        assert not await host.outbound.face_closed(session, member)
        successor = await session.scalar(
            select(AutoOutboundPlate).where(
                AutoOutboundPlate.source_member_id == member_id, AutoOutboundPlate.id != plate_id
            )
        )
        assert (successor is not None) is (next_mode == "valid")
        if successor is not None:
            await host.outbound.advance(session, work_line_id=1, plate_id=successor.id, now_ms=72)
            assert successor.pick_command_code is None
    assert (await host.arm_result(put_code)).status_code == 200
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=80)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=90)
        member = await session.get(AutoOutboundSourceMember, member_id)
        assert await host.outbound.closed(session, plate)
        assert await host.outbound.face_closed(session, member) is (next_mode == "NO_WORK")
