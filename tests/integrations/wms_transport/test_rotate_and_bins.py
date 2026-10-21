"""rotate_rack / move_bins / exchange_bins 的登记:位置与面的有效性交给 WMS 校验(合同 §4.1)。"""

import json

import pytest
from sqlalchemy import select

from app.integrations.wms_transport import (
    BinExchangePair,
    BinMove,
    HandoffPosition,
    IdempotencyConflict,
    InvalidTransportRequest,
    Rack,
    RackBinSlot,
    RackPosition,
)
from app.models import ReliableMessage, TransportTask

from .helpers import caller, counts

CLIENT = "01a13b00-cabd-7390-890a-0c093e7c07a1"


def slot(rack, face, number):
    return RackBinSlot(rack, face, number)


async def stored(session, handle):
    task = await session.scalar(
        select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
    )
    message = await session.scalar(
        select(ReliableMessage).where(ReliableMessage.operation_id == task.operation_id)
    )
    return task, json.loads(message.request_raw)["data"]


def rotate_request(rig, **changes):
    return (
        dict(
            client_request_id=CLIENT,
            caller=caller(rig, "station"),
            rack_id="rack",
            position=RackPosition("KT16"),
            target_face="90",
            rcs_template_id="CK04",
        )
        | changes
    )


async def test_rotate_registers_the_given_position_as_source_and_target(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        handle = await rig.service.rotate_rack(session, **rotate_request(rig))
        await session.commit()
        task, data = await stored(session, handle)
    assert task.kind == "RACK_ROTATE"
    assert data["rcs_template_id"] == "CK04" and data["target_face"] == "90"
    position = {"kind": "RACK_POSITION", "location_code": "KT16"}
    assert data["source"] == data["target"] == position


async def test_rotate_requires_an_explicit_template(transport_storage):
    rig = transport_storage
    request = rotate_request(rig)
    del request["rcs_template_id"]
    async with rig.factory() as session:
        with pytest.raises(TypeError):
            await rig.service.rotate_rack(session, **request)
        assert await counts(session, rig) == (0, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"position": Rack("rack")},
        {"position": Rack("another")},
        {"position": "KT16"},
        {"target_face": ""},
        {"rcs_template_id": "F01"},
    ],
)
async def test_invalid_rotate_leaves_no_task_or_message(transport_storage, changes):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await rig.service.rotate_rack(session, **rotate_request(rig, **changes))
        await session.commit()
        assert await counts(session, rig) == (0, 0)


async def test_rotate_replay_returns_the_original_and_a_changed_position_conflicts(
    transport_storage,
):
    rig = transport_storage
    async with rig.factory() as session:
        original = await rig.service.rotate_rack(session, **rotate_request(rig))
        assert await rig.service.rotate_rack(session, **rotate_request(rig)) == original
        with pytest.raises(IdempotencyConflict):
            await rig.service.rotate_rack(
                session, **rotate_request(rig, position=RackPosition("KT17"))
            )
        await session.commit()
        assert await counts(session, rig) == (1, 1)


async def test_move_bins_registers_sorted_members_without_any_state_lookup(transport_storage):
    rig = transport_storage
    moves = [
        BinMove("b-2", slot("rackB", "B", "1"), HandoffPosition("out")),
        BinMove("b-1", slot("rackA", "A", "1"), HandoffPosition("out")),
    ]
    async with rig.factory() as session:
        handle = await rig.service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig, "station"), moves=moves
        )
        await session.commit()
        task, data = await stored(session, handle)
    assert task.kind == "BIN_MOVE"
    assert [move["container_id"] for move in data["moves"]] == ["b-1", "b-2"]


async def test_move_bins_reordered_replay_returns_the_original(transport_storage):
    rig = transport_storage
    moves = [
        BinMove("b-1", slot("rackA", "A", "1"), HandoffPosition("out")),
        BinMove("b-2", slot("rackA", "A", "2"), HandoffPosition("out")),
    ]
    async with rig.factory() as session:
        original = await rig.service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig, "station"), moves=moves
        )
        replay = await rig.service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig, "station"), moves=moves[::-1]
        )
        assert replay == original and await counts(session, rig) == (1, 1)


async def test_exchange_registers_one_task_for_both_pairs(transport_storage):
    rig = transport_storage
    pairs = [
        BinExchangePair("b-1", slot("rackA", "A", "1"), "b-2", slot("rackB", "A", "1")),
        BinExchangePair("b-3", slot("rackA", "A", "2"), "b-4", slot("rackB", "A", "2")),
    ]
    async with rig.factory() as session:
        handle = await rig.service.exchange_bins(
            session, client_request_id=CLIENT, caller=caller(rig, "station"), exchange_pairs=pairs
        )
        await session.commit()
        task, data = await stored(session, handle)
        assert await counts(session, rig) == (1, 1)
    assert task.kind == "BIN_EXCHANGE" and len(data["moves"]) == 4


async def test_exchange_orientation_replays_and_method_change_conflicts(transport_storage):
    rig = transport_storage
    left, right = slot("rackA", "A", "1"), slot("rackB", "A", "1")
    async with rig.factory() as session:
        original = await rig.service.exchange_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig, "station"),
            exchange_pairs=[BinExchangePair("b-1", left, "b-2", right)],
        )
        swapped = await rig.service.exchange_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig, "station"),
            exchange_pairs=[BinExchangePair("b-2", right, "b-1", left)],
        )
        assert swapped == original
        with pytest.raises(IdempotencyConflict):
            await rig.service.move_bins(
                session,
                client_request_id=CLIENT,
                caller=caller(rig, "station"),
                moves=[BinMove("b-1", left, HandoffPosition("out"))],
            )
        await session.commit()
        assert await counts(session, rig) == (1, 1)
