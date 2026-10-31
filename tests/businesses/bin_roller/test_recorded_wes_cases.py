"""Archived facts adapted to current public ports, not an old-wire replay runner."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.businesses._components.bin_roller.ports import SourceFaceRef, SupplyChange
from app.businesses._components.bin_roller.service import BinRoller
from app.models import ReliableMessage, WorkLine

CASES = json.loads((Path(__file__).parent / "fixtures/wes_recorded_cases.json").read_text())
GOOD = CASES["good"]
BAD = CASES["bad"]


async def configure(flow, task_id):
    flow.current_task = task_id
    async with flow.factory.begin() as session:
        await session.execute(
            WorkLine.__table__.update()
            .where(WorkLine.id == 1)
            .values(
                line_code="KT16",
                position_bindings=dict(FIVE_RACK="KT16", INLET="CNV0301", OUTLET="CNV0302"),
            )
        )


def restart(flow):
    flow.roller = BinRoller(
        flow.roller.context, flow.roller.capabilities, flow.production, tables=flow.store.tables
    )


async def scans(flow, records):
    """Preserve raw codes/times; inject production completion only through its public port."""
    runtime_commands = {}
    for record in records:
        await flow.scan(record["point"], record["raw"], timestamp=record["timestamp"])
        async with flow.factory.begin() as session:
            arrival = next(
                a
                for a in await flow.rows(session, "scan_arrival")
                if a["point"] == record["point"] and a["raw_bin_code"] == record["raw"]
            )
            if record["point"] == "SCAN2":
                assert arrival["command_code"] is None
                await flow.roller.release_scan2(session, arrival_id=arrival["id"])
                await flow.roller.release_scan2(session, arrival_id=arrival["id"])
                arrival = await flow.roller.original_record(session, "scan_arrival", arrival["id"])
            runtime_commands[record["command_code"]] = arrival["command_code"]
        await flow.result(
            arrival["command_code"], record["point"], finish_time=record["finish_time"]
        )
    return runtime_commands


async def returned(flow, archived):
    batch = next(b for b in reversed(await flow.all_rows("return_batch")) if b["pending"])
    data = archived["normalized_payload"]["data"]
    assert [m["bin_code"] for m in batch["members"]][: len(data["moves"])] == [
        m["bin_code"] for m in data["moves"]
    ]
    await flow.decision("return_batch", batch["id"], data)
    await flow.advance("RETURN_BATCH", batch["id"])
    async with flow.factory() as session:
        frozen = await flow.roller.original_record(session, "return_batch", batch["id"])
        assert frozen["manifest"]["moves"] == data["moves"]
        raw = await session.scalar(
            select(ReliableMessage.request_raw).where(
                ReliableMessage.operation_id == frozen["transport_operation_id"]
            )
        )
    expected = [
        dict(
            container_id=m["bin_code"],
            target={
                "kind": m["target"]["type"],
                **{k: m["target"][k] for k in ("rack_id", "rack_face", "slot_id")},
            },
        )
        for m in data["moves"]
    ]
    native = json.loads(raw)["data"]["moves"]
    assert [dict(container_id=m["container_id"], target=m["target"]) for m in native] == expected
    for move in expected:
        await flow.rack_callback(
            dict(
                transport_task_id=frozen["transport_task_id"],
                container_id=move["container_id"],
                milestone="TARGET_PLACED",
                final_position=move["target"],
            ),
            "transport.task.member_position_changed@v1",
        )
    await flow.rack_callback(
        dict(
            transport_task_id=frozen["transport_task_id"],
            kind="BIN_MOVE",
            outcome_revision=1,
            results=[
                dict(container_id=m["container_id"], status="SUCCEEDED", final_position=m["target"])
                for m in expected
            ],
        )
    )
    await flow.advance("RETURN_BATCH", batch["id"])
    return batch["id"]


async def depart(flow, cycle):
    request = next(d for d in reversed(await flow.all_rows("departure")) if d["cycle_id"] == cycle)
    await flow.decision(
        "departure",
        request["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="WH01")),
    )
    await flow.advance("RACK_CYCLE", cycle)
    action = next(
        a
        for a in await flow.all_rows("rack_action")
        if a["cycle_id"] == cycle and a["kind"] == "CTU03"
    )
    await flow.accepted(action)
    await flow.advance("RACK_CYCLE", cycle)
    original_cycle = next(c for c in await flow.all_rows("rack_cycle") if c["id"] == cycle)
    if flow.current_task in (GOOD["task_id"], None):
        step = (
            "MANUAL_PICKING_SOURCE_RACK_OUT"
            if original_cycle["purpose"] == "ORDINARY"
            else "MANUAL_PICKING_RETURN_BUFFER_DRAIN_RACK_OUT"
        )
        matches = [
            t
            for t in GOOD["transports"]
            if t["request_json"].get("rcs_template_id") == "CTU03"
            and t["request_json"]["rack_id"] == original_cycle["rack_id"]
            and t["binding"]["step"] == step
            and t["departure_request"]["data"]["current_face"] == request["request"]["current_face"]
        ]
        assert len(matches) == 1
        archived = matches[0]
        assert archived["transport_task_id"] not in flow.recorded_departures
        flow.recorded_departures[archived["transport_task_id"]] = dict(
            transport_task_id=action["transport_task_id"],
            rack_id=original_cycle["rack_id"],
            purpose=original_cycle["purpose"],
            rack_face=request["request"]["current_face"],
        )
        member = archived["outcome"]["members"][0]
        await flow.rack_callback(
            dict(
                transport_task_id=action["transport_task_id"],
                rack_id=original_cycle["rack_id"],
                kind="RACK_MOVE",
                outcome_revision=1,
                status="SUCCEEDED",
                final_position=member["final_position"],
                arrival_face=member["arrival_face"],
            )
        )
        await flow.advance("RACK_CYCLE", cycle)


async def test_recorded_good_six_bin_shared_flow(roller_lifecycle):
    flow = roller_lifecycle
    await configure(flow, GOOD["task_id"])
    assert GOOD["plan"]["data"]["added_bin_source_racks"][0]["rack_id"] == "510001"
    assert (
        GOOD["inbounds"][0]["normalized_payload"]["data"]["bins"][0]["source_locator"]["rack_id"]
        == "510017"
    )
    command_map = {}
    request_map = {}
    completed_returns = []
    flow.recorded_departures = {}
    # Actual arrival slices preserve the plan separately; concurrent rack windows are not replayed.
    for index, archived in enumerate(GOOD["inbounds"]):
        data = archived["normalized_payload"]["data"]
        member = data["bins"][0]
        locator = member["source_locator"]
        demand = await flow.demand(rack=locator["rack_id"], face=locator["rack_face"])
        await flow.advance("FEED", demand)
        entry = (await flow.all_rows("rack_action"))[-1]
        assert entry["kind"] == "CTU01" and entry["target_face"] == locator["rack_face"]
        await flow.rack_result(entry)
        await flow.advance("RACK_CYCLE", entry["cycle_id"])
        feed = (await flow.all_rows("feed"))[-1]
        request_map[archived["operation_id"]] = feed["operation_id"]
        await flow.decision("feed", feed["id"], data)
        await flow.advance("FEED", demand)
        segment = (await flow.all_rows("feed_segment"))[-1]
        assert segment["members"] == data["bins"]
        await flow.feed_success(segment)
        await flow.advance("FEED", demand)
        if index in (1, 2, 3):
            prior = GOOD["inbounds"][index - 1]["normalized_payload"]["data"]["bins"][0]["bin_code"]
            returned_case = next(
                r
                for r in GOOD["returns"]
                if r["apply_status"] == "APPLIED"
                and r["normalized_payload"]["data"]["moves"][0]["bin_code"] == prior
            )
            completed_returns.append(await returned(flow, returned_case))
        await depart(flow, entry["cycle_id"])
        own_scans = [s for s in GOOD["scans"] if s["raw"].startswith(member["bin_code"] + "-")]
        command_map.update(await scans(flow, own_scans if index < 3 else own_scans[:2]))
        if index == 4:
            # Both recorded returns target the same drain rack, in the original FIFO order.
            for code in ("A000002456", "A000002701"):
                command_map.update(
                    await scans(
                        flow,
                        [
                            s
                            for s in GOOD["scans"]
                            if s["raw"].startswith(code + "-") and s["point"] in ("SCAN3", "SCAN4")
                        ],
                    )
                )
            flow.successor = "NONE"
            await drain_recorded(flow, GOOD["drains"][0], completed_returns)
            flow.successor = "UNKNOWN"
        if index == 5:
            flow.current_task = None
            flow.successor = "NONE"
            restart(flow)
            command_map.update(await scans(flow, own_scans[2:]))
            await drain_recorded(flow, GOOD["drains"][1], completed_returns)
    assert len(command_map) == len(set(command_map.values())) == 24
    assert len(request_map) == len(set(request_map.values())) == 6
    assert len(completed_returns) == 6
    assert all(r["status"] == "COMPLETED" for r in await flow.all_rows("return_round"))
    assert all(c["status"] == "RELEASED" for c in await flow.all_rows("rack_cycle"))
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions].count("CTU01") == 8
    assert [a["kind"] for a in actions].count("CTU03") == 8
    assert not [a for a in actions if a["kind"] == "CTU02"]
    assert len(flow.recorded_departures) == 8
    assert {v["transport_task_id"] for v in flow.recorded_departures.values()} == {
        a["transport_task_id"] for a in actions if a["kind"] == "CTU03"
    }
    assert {
        (v["purpose"], v["rack_face"]): ref
        for ref, v in flow.recorded_departures.items()
        if v["rack_id"] == "510001"
    } == {
        ("ORDINARY", "90"): "transport-1838ce1c-6c40-44fc-940d-d45886eee220",
        ("DRAIN", "270"): "transport-e2f1e84c-0c66-43ea-8e46-e254d8de32f8",
    }
    assert len(await flow.all_rows("feed_segment")) == 6
    async with flow.factory() as session:
        assert len(await flow.messages(session)) == 24
        assert len((await session.execute(select(flow.production_table))).all()) == 6
        assert await flow.roller.return_channel_empty(session, work_line_id=1)


async def drain_recorded(flow, archived, completed_returns):
    scan = next(a for a in reversed(await flow.all_rows("scan_arrival")) if a["point"] == "SCAN4")
    await flow.advance("SCAN", scan["id"])
    drain = (await flow.all_rows("drain"))[-1]
    await flow.decision("drain", drain["id"], archived["normalized_payload"]["data"])
    await flow.advance("DRAIN", drain["id"])
    entry = (await flow.all_rows("rack_action"))[-1]
    assert entry["kind"] == "CTU01"
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    rack = archived["normalized_payload"]["data"]["racks"][0]
    for result in GOOD["returns"]:
        move = result["normalized_payload"]["data"]["moves"][0]
        if (
            result["apply_status"] == "APPLIED"
            and move["target"]["rack_id"] == rack["rack_id"]
            and move["target"]["rack_face"] in rack["rack_face"]
        ):
            completed_returns.append(await returned(flow, result))
    await depart(flow, entry["cycle_id"])


@pytest.mark.parametrize("next_action", ["CTU02", "CTU03"])
@pytest.mark.parametrize("purpose", ["ORDINARY", "DRAIN"])
async def test_recorded_unfinished_return_blocks_original_rack(
    roller_lifecycle, next_action, purpose
):
    flow = roller_lifecycle
    case = BAD["return_before_rotate"]
    await configure(flow, case["task_id"])
    move = case["transport"]["request_json"]["moves"][0]
    target = dict(move["target"])
    target["type"] = target.pop("kind")
    # The fixture has no raw scan. These are explicitly synthetic arrivals of its original bin.
    await flow.scan("SCAN3", move["bin_code"] + "-B", timestamp=10)
    await flow.scan("SCAN4", move["bin_code"] + "-B", timestamp=11)
    scan = (await flow.all_rows("scan_arrival"))[-1]
    await flow.result(scan["command_code"], "SCAN4")
    if purpose == "DRAIN" and next_action == "CTU02":
        # A second real GOOD bin is a synthetic tail to keep this drain's next face needed.
        await flow.scan("SCAN3", "A000002456-B", timestamp=20)
        await flow.scan("SCAN4", "A000002456-B", timestamp=21)
        tail = (await flow.all_rows("scan_arrival"))[-1]
        await flow.result(tail["command_code"], "SCAN4")
    if purpose == "ORDINARY":
        demand = await flow.demand(rack=target["rack_id"], face=target["rack_face"])
        await flow.advance("FEED", demand)
    else:
        flow.successor = "NONE"
        await flow.advance("SCAN", scan["id"])
        drain = (await flow.all_rows("drain"))[0]
        faces = [target["rack_face"], "90"] if next_action == "CTU02" else [target["rack_face"]]
        await flow.decision(
            "drain",
            drain["id"],
            dict(result="READY", racks=[dict(rack_id=target["rack_id"], rack_face=faces)]),
        )
        await flow.advance("DRAIN", drain["id"])
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    if purpose == "ORDINARY":
        if next_action == "CTU02":
            await flow.demand(rack=target["rack_id"], face="90", revision=2)
        feed = (await flow.all_rows("feed"))[0]
        await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
        await flow.advance("FEED", demand)
    batch = (await flow.all_rows("return_batch"))[0]
    await flow.decision(
        "return_batch",
        batch["id"],
        dict(result="READY", moves=[dict(sequence_no=1, bin_code=move["bin_code"], target=target)]),
    )
    await flow.advance("RETURN_BATCH", batch["id"])
    original = (await flow.all_rows("return_batch"))[0]
    await flow.accepted({**original, "operation_id": original["transport_operation_id"]})
    async with flow.factory() as session:
        from app.integrations.wms_transport import TransportCaller

        accepted = await flow.roller.capabilities.transport.get_result(
            session,
            transport_task_id=original["transport_task_id"],
            client_request_id=original["client_request_id"],
            caller=TransportCaller(original["caller_workline_id"], original["station_id"]),
        )
        assert accepted.submission_response_state == "VALID"
        assert accepted.submission_response_code == "RECEIVED" and accepted.outcome is None
        decision = await flow.roller.capabilities.wms.get_result(
            session,
            message_id=original["message_id"],
            operation="outbound.bin.return_batch@v1",
            operation_id=original["operation_id"],
        )
        assert decision.response_state == "VALID" and decision.response_code == "DECIDED"
        assert decision.response_data == dict(
            result="READY", moves=[dict(sequence_no=1, bin_code=move["bin_code"], target=target)]
        )
    for _ in range(2):
        restart(flow)
        await flow.advance("RACK_CYCLE", entry["cycle_id"])
        await flow.advance("RETURN_BATCH", batch["id"])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]
    # Controlled completion stimulus; the source archive itself never captured this success.
    await flow.success(batch["id"], placed=False)
    await flow.advance("RETURN_BATCH", batch["id"])
    assert [a["kind"] for a in await flow.all_rows("rack_action")] == ["CTU01"]
    await flow.success(batch["id"], placed=True)
    await flow.advance("RETURN_BATCH", batch["id"])
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    if purpose == "DRAIN" and next_action == "CTU02":
        await flow.complete_return(no_batch=True)
    if next_action == "CTU03":
        await depart(flow, entry["cycle_id"])
    actions = await flow.all_rows("rack_action")
    assert [a["kind"] for a in actions] == ["CTU01", next_action]
    finished = (await flow.all_rows("return_batch"))[0]
    for key in ("operation_id", "transport_task_id", "client_request_id", "manifest"):
        assert finished[key] == original[key]
    await flow.success(batch["id"], placed=True)
    await flow.advance("RETURN_BATCH", batch["id"])
    assert [a["id"] for a in await flow.all_rows("rack_action")] == [a["id"] for a in actions]


async def test_recorded_cancelled_arrival_consumes_face_done_once(roller_lifecycle):
    flow = roller_lifecycle
    case = BAD["cancelled_face_done"]
    await configure(flow, case["task_id"])
    demand = await flow.demand(rack=case["rack_id"], face=case["rack_face"])
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    async with flow.factory.begin() as session:
        await flow.roller.apply_supply_change(
            session,
            SupplyChange(
                1,
                case["task_id"],
                str(case["cancellation_evidence"]),
                (),
                (SourceFaceRef(1, case["rack_id"], case["rack_face"]),),
            ),
        )
    await flow.rack_result(entry)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", demand)
    restart(flow)
    await flow.advance("FEED", demand)
    assert len(await flow.all_rows("feed")) == 1
    assert not await flow.all_rows("feed_segment")
    await depart(flow, entry["cycle_id"])
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"


async def test_recorded_scan2_delegates_only_its_own_arrival(roller_application):
    flow = roller_application
    host = flow.hosts["first"]
    earlier = BAD["out_of_order_scan2"]
    cancelled = BAD["cancelled_scan2"]["scan"]
    # No batch-membership oracle is installed: only the production-port isolation is checked.
    await flow.application_scan("first", "SCAN2", earlier["other_raw"], timestamp=1)
    async with flow.factory() as session:
        unrelated = dict((await session.execute(select(flow.production_table))).mappings().one())
    for record in (earlier, cancelled):
        await flow.application_scan("first", "SCAN2", record["raw"], timestamp=record["timestamp"])
        await flow.application_scan("first", "SCAN2", record["raw"], timestamp=record["timestamp"])
        async with flow.factory() as session:
            own = next(
                a
                for a in await flow.rows(session, "scan_arrival")
                if a["raw_bin_code"] == record["raw"]
            )
            assert own["command_code"] is None
        await host.complete_production(own["id"])
        await host.complete_production(own["id"])
    async with flow.factory() as session:
        rows = (await session.execute(select(flow.production_table))).mappings().all()
        assert len(rows) == 3
        assert (
            dict(next(r for r in rows if r["arrival_id"] == unrelated["arrival_id"])) == unrelated
        )
        assert len(await flow.messages(session)) == 2


async def test_recorded_scan1_needs_no_target_or_inbound_gate(roller_scans):
    flow = roller_scans
    record = next(s for s in GOOD["scans"] if s["raw"] == "A000002165-B" and s["point"] == "SCAN1")
    await flow.scan("SCAN1", record["raw"], timestamp=record["timestamp"])
    async with flow.factory() as session:
        arrival = (await flow.rows(session, "scan_arrival"))[0]
        command = await flow.command(session, arrival["command_code"])
        assert command.task_type == "MOVE_FORWARD"
        assert not await flow.rows(session, "feed_segment")
        assert not await flow.rows(session, "rack_action")


async def test_recorded_missing_scan4_result_keeps_fifo_until_original_success(roller_scans):
    flow = roller_scans
    first = BAD["scan4_missing_result"]["raw"]
    second = "A000002701-B"  # GOOD's next real return; combined fault injection, not one raw trace.
    for i, raw in enumerate((first, second)):
        await flow.scan("SCAN3", raw, timestamp=10 + i * 10)
        await flow.scan("SCAN4", raw, timestamp=11 + i * 10)
    async with flow.factory() as session:
        arrivals = [a for a in await flow.rows(session, "scan_arrival") if a["point"] == "SCAN4"]
    await flow.result(arrivals[1]["command_code"], "SCAN4")
    for _ in range(2):
        flow.rebuild()
        async with flow.factory() as session:
            assert await flow.roller.get_return_candidates(session, work_line_id=1) == ()
            assert not await flow.roller.return_channel_empty(session, work_line_id=1)
    await flow.result(arrivals[0]["command_code"], "SCAN4", finish_time=101)
    await flow.result(arrivals[0]["command_code"], "SCAN4", finish_time=101)
    async with flow.factory() as session:
        candidates = await flow.roller.get_return_candidates(session, work_line_id=1)
        assert [r["bin_code"] for r in candidates] == [first[:-2], second[:-2]]
        assert len(await flow.rows(session, "return_round")) == 2
        assert len(await flow.messages(session)) == 4


@pytest.mark.parametrize("point", ["SCAN1", "SCAN2", "SCAN3", "SCAN4"])
async def test_recorded_scan_replay_keeps_frozen_original_command(roller_scans, point):
    flow = roller_scans
    record = next(
        s for s in GOOD["scans"] if s["raw"].startswith("A000002701-") and s["point"] == point
    )
    if point == "SCAN4":
        await flow.scan("SCAN3", "A000002701-B", timestamp=record["timestamp"] - 1)
    await flow.scan(point, record["raw"], timestamp=record["timestamp"])
    async with flow.factory.begin() as session:
        arrival = next(a for a in await flow.rows(session, "scan_arrival") if a["point"] == point)
        if point == "SCAN2":
            await flow.roller.release_scan2(session, arrival_id=arrival["id"])
            arrival = await flow.roller.original_record(session, "scan_arrival", arrival["id"])
        command = await flow.command(session, arrival["command_code"])
        frozen = (command.command_code, command.expires_at, command.request_raw)
    await flow.result(arrival["command_code"], point, finish_time=record["finish_time"])
    flow.rebuild()
    await flow.scan(point, record["raw"], timestamp=record["timestamp"])
    async with flow.factory.begin() as session:
        if point == "SCAN2":
            await flow.roller.release_scan2(session, arrival_id=arrival["id"])
        command = await flow.command(session, arrival["command_code"])
        assert (command.command_code, command.expires_at, command.request_raw) == frozen
        assert (
            len([a for a in await flow.rows(session, "scan_arrival") if a["point"] == point]) == 1
        )
        assert len(await flow.messages(session)) == (2 if point == "SCAN4" else 1)


async def test_recorded_departure_native_ack_wakes_and_releases_without_result(
    roller_lifecycle, monkeypatch
):
    from app import business_wakeups
    from app.businesses import catalog
    from app.http_client import DeliveryState, HttpResult
    from app.integrations.wms_transport import ADAPTERS, PROTOCOL, TransportCaller
    from app.reliable.store import AttemptOutcome, MessageStore
    from app.reliable.types import MessageStatus

    flow = roller_lifecycle
    await configure(flow, GOOD["task_id"])
    archived = next(
        r
        for r in GOOD["inbounds"]
        if r["normalized_payload"]["data"]["bins"][0]["bin_code"] == "A000002456"
    )
    data = archived["normalized_payload"]["data"]
    locator = data["bins"][0]["source_locator"]
    demand = await flow.demand(rack=locator["rack_id"], face=locator["rack_face"])
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(entry)
    await flow.advance("FEED", demand)
    feed = (await flow.all_rows("feed"))[0]
    await flow.decision("feed", feed["id"], data)
    await flow.advance("FEED", demand)
    await flow.feed_success((await flow.all_rows("feed_segment"))[0])
    await flow.advance("FEED", demand)
    departure = (await flow.all_rows("departure"))[0]
    await flow.decision(
        "departure",
        departure["id"],
        dict(result="READY", rack_destination=dict(type="ZONE", location_code="WH01")),
    )
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    action = (await flow.all_rows("rack_action"))[-1]
    assert action["kind"] == "CTU03"
    for composition in catalog.entries():
        composition.PROGRESSION = "scan_pending"
    catalog.validate_business_contracts()
    notices = []
    monkeypatch.setattr(business_wakeups, "enqueue_business", notices.append)
    store = MessageStore(
        flow.factory,
        on_result=business_wakeups.wms_result_committed,
        on_delivered=business_wakeups.transport_submit_delivered,
    )
    transport = flow.roller.capabilities.transport
    claims = await store.claim(100, transport.outbox.targets)
    claim = next(c for c in claims if c.message.operation_id == action["operation_id"])
    assert notices == []
    raw = json.dumps(
        dict(
            operation_id=action["operation_id"],
            code="RECEIVED",
            timestamp=1,
            data=dict(transport_task_id=action["transport_task_id"]),
        )
    ).encode()
    response = HttpResult(DeliveryState.HTTP_RESPONSE_COMPLETE, None, 202, (), raw)
    verdict = ADAPTERS[PROTOCOL].classify_response(claim.message, response)
    assert await store.finish(
        claim, AttemptOutcome(MessageStatus.DONE, result=response, verdict=verdict)
    )
    assert notices == ["first"]
    async with flow.factory() as session:
        view = await transport.get_result(
            session,
            transport_task_id=action["transport_task_id"],
            client_request_id=action["client_request_id"],
            caller=TransportCaller(action["caller_workline_id"], action["station_id"]),
        )
        assert view.outcome is None
    restart(flow)
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    await flow.advance("RACK_CYCLE", entry["cycle_id"])
    assert (await flow.all_rows("rack_cycle"))[0]["status"] == "RELEASED"
    assert len(await flow.all_rows("rack_action")) == 2
    assert notices == ["first"]


async def test_recorded_new_cycle_ignores_closed_cycle_replayed_acceptance(roller_lifecycle):
    flow = roller_lifecycle
    case = BAD["closed_history"]
    await configure(flow, "isolated-closed-history")
    old_demand = await flow.demand(rack=case["rack_id"], face=case["rack_face"])
    await flow.advance("FEED", old_demand)
    old_entry = (await flow.all_rows("rack_action"))[0]
    await flow.rack_result(old_entry)
    await flow.advance("FEED", old_demand)
    feed = (await flow.all_rows("feed"))[0]
    # Controlled closed-cycle history, not conversion of archived RECONCILING to success.
    await flow.decision("feed", feed["id"], dict(result="RACK_FACE_DONE"))
    await flow.advance("FEED", old_demand)
    await depart(flow, old_entry["cycle_id"])
    old_departure = (await flow.all_rows("rack_action"))[-1]
    old_snapshot = await flow.all_rows("rack_cycle")
    flow.current_task = case["task_id"]
    demand = await flow.demand(rack=case["rack_id"], face=case["rack_face"], revision=8)
    await flow.advance("FEED", demand)
    entry = (await flow.all_rows("rack_action"))[-1]
    assert entry["cycle_id"] != old_entry["cycle_id"]
    await flow.rack_result(entry)
    await flow.advance("FEED", demand)
    new_feed = (await flow.all_rows("feed"))[-1]
    await flow.decision(
        "feed",
        new_feed["id"],
        dict(
            result="READY",
            bins=[
                dict(
                    bin_code=case["bin_code"],
                    source_locator=dict(
                        type="RACK_BIN_SLOT",
                        rack_id=case["rack_id"],
                        rack_face=case["rack_face"],
                        slot_id=case["slot_id"],
                    ),
                )
            ],
        ),
    )
    await flow.advance("FEED", demand)
    await flow.accepted(old_departure)
    await flow.advance("RACK_CYCLE", old_entry["cycle_id"])
    cycles = await flow.all_rows("rack_cycle")
    assert dict(cycles[0]) == dict(old_snapshot[0])
    assert cycles[1]["id"] == entry["cycle_id"] and cycles[1]["status"] == "ACTIVE"
    assert len(await flow.all_rows("feed_segment")) == 1
    assert (await flow.all_rows("feed_segment"))[0]["members"][0]["bin_code"] == case["bin_code"]
