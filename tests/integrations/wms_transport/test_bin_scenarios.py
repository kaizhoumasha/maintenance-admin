"""料箱投料 / 回架 / 交换的业务接入:方向由场景固定,合同规则由能力层执行(合同 §3.2、§8)。"""

import json

import pytest
from sqlalchemy import select

from app.integrations.wms_transport import (
    BinExchangePair,
    BinMove,
    BinScenarios,
    HandoffPosition,
    IdempotencyConflict,
    InvalidTransportRequest,
    RackBinSlot,
    TransportService,
)
from app.models import TransportTask

from .helpers import caller, counts, deterministic_uuid7, frozen_requests, golden_head

CLIENT = "01a13b00-cabd-7390-890a-0c093e7c07a1"
OTHER = "01a13b00-cabd-7390-890a-0c093e7c07a2"
IN, OUT = HandoffPosition("IN-1"), HandoffPosition("OUT-1")


def slot(rack="510046", face="90", number="S1"):
    return RackBinSlot(rack, face, number)


def scenarios_for(rig, **service_options):
    return BinScenarios(
        TransportService(rig.outbox, target=rig.target.name, clock=rig.clock, **service_options)
    )


def data_of(raw):
    return json.loads(raw)["data"]


async def test_feed_moves_bins_from_rack_slots_to_the_feed_handoff(transport_storage):
    rig = transport_storage
    moves = [BinMove("b-2", slot("510047", "270", "S2"), IN), BinMove("b-1", slot(), IN)]
    async with rig.factory() as session:
        handle = await scenarios_for(rig).feed_bins(
            session, client_request_id=CLIENT, caller=caller(rig), moves=moves
        )
        (raw,) = await frozen_requests(session, rig)
        task = await session.scalar(
            select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
        )
    data = data_of(raw)
    assert task.kind == "BIN_MOVE" and data["kind"] == "BIN_MOVE"
    assert [move["container_id"] for move in data["moves"]] == ["b-1", "b-2"]
    assert data["moves"][0] == {
        "container_id": "b-1",
        "source": {
            "kind": "RACK_BIN_SLOT",
            "rack_id": "510046",
            "rack_face": "90",
            "slot_id": "S1",
        },
        "target": {"kind": "HANDOFF_POSITION", "location_code": "IN-1"},
    }


async def test_return_moves_bins_from_the_return_handoff_into_rack_slots(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        await scenarios_for(rig).return_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            moves=[BinMove("b-1", OUT, slot())],
        )
        (raw,) = await frozen_requests(session, rig)
    (move,) = data_of(raw)["moves"]
    assert move["source"] == {"kind": "HANDOFF_POSITION", "location_code": "OUT-1"}
    assert move["target"]["kind"] == "RACK_BIN_SLOT" and move["target"]["slot_id"] == "S1"


@pytest.mark.parametrize("scenario", ["feed_bins", "return_bins"])
@pytest.mark.parametrize(
    "move",
    [
        BinMove("b-1", slot("510046", "90", "S1"), slot("510046", "90", "S2")),
        BinMove("b-1", IN, OUT),
        "not-a-move",
        None,
    ],
    ids=["slot-to-slot", "handoff-to-handoff", "text", "none"],
)
async def test_a_move_in_the_wrong_shape_is_rejected_without_writes(
    transport_storage, scenario, move
):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await getattr(scenarios_for(rig), scenario)(
                session, client_request_id=CLIENT, caller=caller(rig), moves=[move]
            )
        await session.commit()
        assert await counts(session, rig) == (0, 0)


@pytest.mark.parametrize(
    "scenario,wrong",
    [
        ("feed_bins", BinMove("b-1", OUT, slot())),
        ("return_bins", BinMove("b-1", slot(), IN)),
    ],
)
async def test_each_scenario_accepts_only_its_own_direction(transport_storage, scenario, wrong):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await getattr(scenarios_for(rig), scenario)(
                session, client_request_id=CLIENT, caller=caller(rig), moves=[wrong]
            )
        assert await counts(session, rig) == (0, 0)


@pytest.mark.parametrize("scenario", ["feed_bins", "return_bins"])
@pytest.mark.parametrize("moves", [[], "x", None])
async def test_the_member_list_is_validated_before_any_write(transport_storage, scenario, moves):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await getattr(scenarios_for(rig), scenario)(
                session, client_request_id=CLIENT, caller=caller(rig), moves=moves
            )
        assert await counts(session, rig) == (0, 0)


async def test_capability_rules_still_apply_through_the_scenarios(transport_storage):
    rig = transport_storage
    crowded = [BinMove(f"b-{n}", slot(number=f"S{n}"), IN) for n in range(5)]
    mixed_faces = [
        BinMove("b-1", slot(face="90"), IN),
        BinMove("b-2", slot(face="270", number="S2"), IN),
    ]
    async with rig.factory() as session:
        for moves in (crowded, mixed_faces):
            with pytest.raises(InvalidTransportRequest):
                await scenarios_for(rig).feed_bins(
                    session, client_request_id=CLIENT, caller=caller(rig), moves=moves
                )
        assert await counts(session, rig) == (0, 0)


async def test_exchange_registers_one_coordinated_task(transport_storage):
    rig = transport_storage
    pairs = [
        BinExchangePair("b-1", slot("510046", "90", "S1"), "b-2", slot("510047", "90", "S1")),
        BinExchangePair("b-3", slot("510046", "90", "S2"), "b-4", slot("510047", "90", "S2")),
    ]
    async with rig.factory() as session:
        handle = await scenarios_for(rig).exchange_bins(
            session, client_request_id=CLIENT, caller=caller(rig), exchange_pairs=pairs
        )
        (raw,) = await frozen_requests(session, rig)
        assert await counts(session, rig) == (1, 1)
    data = data_of(raw)
    assert data["kind"] == "BIN_EXCHANGE" and len(data["moves"]) == 4
    assert handle.transport_task_id == data["transport_task_id"]


async def test_replay_returns_the_original_and_a_different_batch_conflicts(transport_storage):
    rig = transport_storage
    scenarios = scenarios_for(rig)
    first = [BinMove("b-1", slot(), IN)]
    async with rig.factory() as session:
        original = await scenarios.feed_bins(
            session, client_request_id=CLIENT, caller=caller(rig), moves=first
        )
        assert (
            await scenarios.feed_bins(
                session, client_request_id=CLIENT, caller=caller(rig), moves=first
            )
            == original
        )
        with pytest.raises(IdempotencyConflict):
            await scenarios.return_bins(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                moves=[BinMove("b-1", OUT, slot())],
            )
        await session.commit()
        assert await counts(session, rig) == (1, 1)


@pytest.mark.parametrize("commit", [True, False], ids=["commit", "rollback"])
async def test_obligations_share_the_callers_transaction(transport_storage, commit):
    rig = transport_storage
    async with rig.factory() as session:
        await scenarios_for(rig).feed_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            moves=[BinMove("b-1", slot(), IN)],
        )
        await (session.commit() if commit else session.rollback())
    async with rig.factory() as session:
        assert await counts(session, rig) == ((1, 1) if commit else (0, 0))


def test_module_level_scenarios_share_the_module_level_transport_service():
    from app.integrations.wms_transport import bin_scenarios, transport_service

    assert isinstance(bin_scenarios, BinScenarios)
    assert bin_scenarios.transport is transport_service


S1_90 = b'{"kind":"RACK_BIN_SLOT","rack_id":"510046","rack_face":"90","slot_id":"S1"}'
IN_1 = b'{"kind":"HANDOFF_POSITION","location_code":"IN-1"}'
GOLDEN_FEED = (
    golden_head(b"000000000001", b"000000000002")
    + b'"kind":"BIN_MOVE","moves":['
    + b'{"container_id":"b-1","source":'
    + S1_90
    + b',"target":'
    + IN_1
    + b"},"
    + b'{"container_id":"b-2","source":'
    + b'{"kind":"RACK_BIN_SLOT","rack_id":"510047","rack_face":"270","slot_id":"S2"},'
    + b'"target":'
    + IN_1
    + b"}]}}"
)
GOLDEN_RETURN = (
    golden_head(b"000000000003", b"000000000004")
    + b'"kind":"BIN_MOVE","moves":[{"container_id":"b-1","source":'
    + b'{"kind":"HANDOFF_POSITION","location_code":"OUT-1"},"target":'
    + S1_90
    + b"}]}}"
)
S1_B = b'{"kind":"RACK_BIN_SLOT","rack_id":"510047","rack_face":"90","slot_id":"S1"}'
GOLDEN_EXCHANGE = (
    golden_head(b"000000000005", b"000000000006")
    + b'"kind":"BIN_EXCHANGE","moves":['
    + b'{"container_id":"b-1","source":'
    + S1_90
    + b',"target":'
    + S1_B
    + b"},"
    + b'{"container_id":"b-2","source":'
    + S1_B
    + b',"target":'
    + S1_90
    + b"}]}}"
)


async def test_feed_return_and_exchange_freeze_the_exact_contract_bytes(transport_storage):
    rig = transport_storage
    scenarios = scenarios_for(rig, uuid7=deterministic_uuid7(rig))
    async with rig.factory() as session:
        await scenarios.feed_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            moves=[BinMove("b-2", slot("510047", "270", "S2"), IN), BinMove("b-1", slot(), IN)],
        )
        await scenarios.return_bins(
            session,
            client_request_id=OTHER,
            caller=caller(rig),
            moves=[BinMove("b-1", OUT, slot())],
        )
        await scenarios.exchange_bins(
            session,
            client_request_id="01a13b00-cabd-7390-890a-0c093e7c07a3",
            caller=caller(rig),
            exchange_pairs=[
                BinExchangePair(
                    "b-1", slot("510046", "90", "S1"), "b-2", slot("510047", "90", "S1")
                )
            ],
        )
        assert await frozen_requests(session, rig) == [GOLDEN_FEED, GOLDEN_RETURN, GOLDEN_EXCHANGE]
