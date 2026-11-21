# ruff: noqa: F405
from tests.businesses.smt_auto._outbound_support import *  # noqa: F401,F403


@pytest.mark.parametrize(
    "wait,disabled,cancel",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
@pytest.mark.parametrize("legacy_diameter", [False, True])
async def test_original_bin_pick_put_report_survives_wait_and_disable(
    outbound_host, wait, disabled, cancel, legacy_diameter, monkeypatch
):
    from app.businesses.smt_auto.ecs_command import PickAndPut

    builder = PickAndPut.from_millimeters
    if legacy_diameter:
        # Create frozen commands using the builder used before diameter conversion.
        monkeypatch.setattr(
            PickAndPut, "from_millimeters", classmethod(lambda cls, **params: cls(**params))
        )
    host = outbound_host
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.start_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=20
        )
    await host.settle(
        "outbound.bin.work_plan@v1",
        dict(
            result="READY",
            cell_id="C1",
            cell_index=1,
            bin_type=8,
            reel_layer=2,
            reel_thickness=15.3,
            reel_diameter=179,
        ),
    )
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=30
        )
        plate = await session.scalar(select(AutoOutboundPlate))
        plate_id = plate.id
        if cancel:
            (await session.get(AutoTask, "t")).status = "CANCELLED"
        if disabled:
            line = await session.get(WorkLine, 1)
            bindings = line.device_bindings
            line.device_bindings = {k: v for k, v in bindings.items() if k != "PLATFORM_SCANNER"}
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=35)
            assert plate.pick_command_code is None
            line.device_bindings = bindings
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=40)
        assert plate.pick_command_code
        pick_code = plate.pick_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == pick_code,
            )
        )
        assert (
            json.loads(await read_history_body(session, (command).request_record_id))["params"][
                "source"
            ]["reel_totalthickness"]
            == 30.6
        )
        raw = json.loads(await read_history_body(session, command.request_record_id))
        assert raw["params"]["source"]["bin_type"] == "8"
        assert raw["params"]["source"]["reel_diameter"] == (179 if legacy_diameter else 7)
        assert plate.source_params["reel_diameter"] == 179
        if legacy_diameter:
            snapshot = plate.source_arrival_snapshot
            command = {
                k: v for k, v in snapshot["pick_command"].items() if k != "platform_scanner_id"
            }
            plate.source_arrival_snapshot = {**snapshot, "pick_command": command}
    if legacy_diameter and disabled:
        # A pending command predating the split can still use its original Result's scan.
        assert (await host.arm_result(pick_code, legacy_scan=True)).status_code == 200
    else:
        if wait:
            assert (await host.platform_scan(pick_code)).status_code == 200
        else:
            assert (
                await host.arm_result(pick_code, legacy_scan=not legacy_diameter)
            ).status_code == 200
            wrong_pick = await host.platform_scan("wrong-pick-command")
            assert wrong_pick.diagnostic[0]["reason_code"] == "PLATFORM_SCAN_PICK_NOT_FOUND"
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=45)
            assert not await session.scalar(
                select(AutoOutboundDecisionRound.id).where(
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE"
                )
            )
        if wait:
            assert (await host.arm_result(pick_code)).status_code == 200
        else:
            assert (await host.platform_scan(pick_code)).status_code == 200
        assert (await host.platform_scan(pick_code)).outcome == "duplicate"
        conflict = await host.platform_scan(pick_code, barcode="different")
        assert conflict.diagnostic[0]["reason_code"] == "PLATFORM_SCAN_CONFLICT"
    async with host.factory.begin() as session:
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=50)
        rounds = (
            await session.scalars(
                select(AutoOutboundDecisionRound).where(
                    AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE"
                )
            )
        ).all()
        assert len(rounds) == 1
        assert rounds[0].request_data["barcode"] == "原始料盘," * 60
        assert rounds[0].request_data["scanned_at"] == 88
    if wait:
        await host.settle(
            "outbound.bin.material.decide@v1", dict(result="WAIT", retry_after_ms=100)
        )
        async with host.factory.begin() as session:
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=60)
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=159)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AutoOutboundDecisionRound)
                    .where(AutoOutboundDecisionRound.purpose == "MATERIAL_DECIDE")
                )
                == 1
            )
            await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=160)
    await host.settle(
        "outbound.bin.material.decide@v1",
        dict(
            result="ACCEPT",
            target_locator=dict(
                type="RACK_SLOT",
                rack_id="target",
                rack_face="A",
                slot_id="S1",
                rack_layer=3,
                rack_column=4,
            ),
        ),
    )
    async with host.factory.begin() as session:
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=170)
        assert plate.put_command_code
        put_code = plate.put_command_code
        command = await session.scalar(
            select(IntegrationExchange).where(
                IntegrationExchange.role == "ECS_COMMAND",
                IntegrationExchange.command_code == put_code,
            )
        )
        raw = json.loads(await read_history_body(session, command.request_record_id))
        assert raw["params"]["target"]["reel_diameter"] == (179 if legacy_diameter else 7)
        assert raw["params"]["target"]["rack_side"] == "A"
        if legacy_diameter:
            snapshot = plate.source_arrival_snapshot
            put = snapshot["put_command"]
            legacy_target = dict(put["params"]["target"])
            legacy_target["rack_face"] = legacy_target.pop("rack_side")
            plate.source_arrival_snapshot = {
                **snapshot,
                "put_command": {**put, "params": {**put["params"], "target": legacy_target}},
            }
        if disabled:
            (await session.get(WorkLine, 1)).is_enabled = False
    monkeypatch.setattr(PickAndPut, "from_millimeters", builder)
    await host.settle("outbound.bin.work_plan@v1", {"result": "NO_WORK"})
    assert (await host.arm_result(put_code, location=None)).status_code == 200
    async with host.factory.begin() as session:
        work = await session.get(AutoBinWork, host.work_id)
        await host.work_plans.consume_bin(
            session, work_line_id=1, arrival_id=work.arrival_id, now_ms=179
        )
        plate = await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=180)
        assert plate.report_message_id
        assert plate.report_data["occurred_at"] == 99
        ports = AutoRackPorts(host.dispatch, outbound=host.outbound)
        demand, handoff = await ports.facts(session, job_id=plate.target_rack_job_id)
        assert demand.state == "NONE"
        assert handoff.state == "CLEAR"
        assert plate.report_data["source_locator"] == dict(
            type="BIN_CELL", bin_code="A000000001", cell_id="C1"
        )
        assert not await host.outbound.closed(session, plate)
    await host.settle("outbound.material.movement_report@v1", {}, "RECORDED")
    async with host.factory.begin() as session:
        original = await session.get(AutoOutboundPlate, plate_id)
        assert not await host.outbound.closed(session, original)
        snapshot = original.source_arrival_snapshot
        command = snapshot["put_command"]
        params = command["params"]
        original.source_arrival_snapshot = {
            **snapshot,
            "put_command": {
                **command,
                "params": {**params, "target": {**params["target"], "reel_diameter": 15}},
            },
        }
        await host.outbound.advance_report(session, plate_id=plate_id, now_ms=189)
        assert original.report_consumed_at_ms is None
        assert not await host.outbound.closed(session, original)
        original.source_arrival_snapshot = snapshot
        plate = await host.outbound.advance_report(session, plate_id=plate_id, now_ms=190)
        assert await host.outbound.closed(session, plate)
        assert not plate.pending
        work = await session.get(AutoBinWork, host.work_id)
        assert await host.outbound.bin_closed(session, work)
        await host.outbound.advance(session, work_line_id=1, plate_id=plate_id, now_ms=200)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IntegrationExchange)
                .where(IntegrationExchange.role == "ECS_COMMAND")
            )
            == 2
        )
