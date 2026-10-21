"""rotate_rack / move_bins / exchange_bins:依赖可信的货架位置与面,缺失即失败关闭(合同 §3.2)。"""

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
    RackState,
    RackStateUnavailable,
    TransportCaller,
    TransportService,
)
from app.models import ReliableMessage, TransportTask

from .helpers import counts

CLIENT = "01a13b00-cabd-7390-890a-0c093e7c07a1"


class StateBook:
    def __init__(self, **states):
        self.states, self.calls = states, []

    async def rack_state(self, rack_id):
        self.calls.append(rack_id)
        return self.states.get(rack_id)


def service_with(rig, reader):
    return TransportService(
        rig.outbox, target=rig.target.name, clock=rig.clock, state_reader=reader
    )


def caller(rig):
    return TransportCaller(rig.target.name, "station")


def slot(rack, face, number):
    return RackBinSlot(rack, face, number)


async def stored(session, rig, handle):
    task = await session.scalar(
        select(TransportTask).where(TransportTask.transport_task_id == handle.transport_task_id)
    )
    message = await session.scalar(
        select(ReliableMessage).where(ReliableMessage.operation_id == task.operation_id)
    )
    return task, json.loads(message.request_raw)["data"]


async def test_rotate_registers_with_the_trusted_exact_position(transport_storage):
    rig = transport_storage
    service = service_with(rig, StateBook(rack=RackState("KT16", "0")))
    async with rig.factory() as session:
        handle = await service.rotate_rack(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            rack_id="rack",
            position=Rack("rack"),
            target_face="90",
            rcs_template_id="CK04",
        )
        await session.commit()
        task, data = await stored(session, rig, handle)
    assert task.kind == "RACK_ROTATE"
    assert data["rcs_template_id"] == "CK04" and data["target_face"] == "90"
    assert data["source"] == {"kind": "RACK", "location_code": "rack"}
    assert data["target"] == {"kind": "RACK_POSITION", "location_code": "KT16"}


@pytest.mark.parametrize(
    "reader,position,face",
    [
        (None, Rack("rack"), "90"),
        (StateBook(), Rack("rack"), "90"),
        (StateBook(rack=RackState("KT16", "90")), Rack("rack"), "90"),
        (StateBook(rack=RackState("KT16", "0")), RackPosition("KT17"), "90"),
    ],
    ids=["no-reader", "unknown-rack", "same-face", "position-mismatch"],
)
async def test_rotate_fails_closed_without_trusted_state(transport_storage, reader, position, face):
    rig = transport_storage
    service = service_with(rig, reader)
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await service.rotate_rack(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                rack_id="rack",
                position=position,
                target_face=face,
                rcs_template_id="CTU02",
            )
        await session.commit()
        assert await counts(session, rig) == (0, 0)


async def test_missing_state_is_reported_as_unavailable(transport_storage):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(RackStateUnavailable):
            await service_with(rig, None).move_bins(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                moves=[BinMove("b", slot("rackA", "A", "1"), HandoffPosition("out"))],
            )


async def test_bins_register_only_when_every_face_matches_the_trusted_face(transport_storage):
    rig = transport_storage
    book = StateBook(rackA=RackState("P1", "A"), rackB=RackState("P2", "B"))
    service = service_with(rig, book)
    moves = [
        BinMove("b-2", slot("rackB", "B", "1"), HandoffPosition("out")),
        BinMove("b-1", slot("rackA", "A", "1"), HandoffPosition("out")),
    ]
    async with rig.factory() as session:
        handle = await service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig), moves=moves
        )
        await session.commit()
        task, data = await stored(session, rig, handle)
    assert task.kind == "BIN_MOVE" and sorted(book.calls) == ["rackA", "rackB"]
    assert [move["container_id"] for move in data["moves"]] == ["b-1", "b-2"]


@pytest.mark.parametrize("states", [{"rackA": RackState("P1", "B")}, {}], ids=["face", "unknown"])
async def test_bins_fail_closed_on_wrong_or_unknown_face(transport_storage, states):
    rig = transport_storage
    async with rig.factory() as session:
        with pytest.raises(InvalidTransportRequest):
            await service_with(rig, StateBook(**states)).move_bins(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                moves=[BinMove("b", slot("rackA", "A", "1"), HandoffPosition("out"))],
            )
        await session.commit()
        assert await counts(session, rig) == (0, 0)


async def test_exchange_registers_one_task_for_both_pairs(transport_storage):
    rig = transport_storage
    book = StateBook(rackA=RackState("P1", "A"), rackB=RackState("P2", "A"))
    pairs = [
        BinExchangePair("b-1", slot("rackA", "A", "1"), "b-2", slot("rackB", "A", "1")),
        BinExchangePair("b-3", slot("rackA", "A", "2"), "b-4", slot("rackB", "A", "2")),
    ]
    async with rig.factory() as session:
        handle = await service_with(rig, book).exchange_bins(
            session, client_request_id=CLIENT, caller=caller(rig), exchange_pairs=pairs
        )
        await session.commit()
        task, data = await stored(session, rig, handle)
        assert await counts(session, rig) == (1, 1)
    assert task.kind == "BIN_EXCHANGE" and len(data["moves"]) == 4


async def test_replay_returns_the_original_without_reading_current_state(transport_storage):
    rig = transport_storage
    book = StateBook(rackA=RackState("P1", "A"))
    service = service_with(rig, book)
    first_moves = [
        BinMove("b-1", slot("rackA", "A", "1"), HandoffPosition("out")),
        BinMove("b-2", slot("rackA", "A", "2"), HandoffPosition("out")),
    ]
    async with rig.factory() as session:
        original = await service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig), moves=first_moves
        )
        await session.commit()
    calls = len(book.calls)
    book.states["rackA"] = RackState("P1", "B")  # 现场面已变:重放仍须返回原任务
    async with rig.factory() as session:
        replay = await service.move_bins(
            session, client_request_id=CLIENT, caller=caller(rig), moves=first_moves[::-1]
        )
        assert replay == original and len(book.calls) == calls
        assert await counts(session, rig) == (1, 1)


async def test_exchange_orientation_replays_and_method_change_conflicts(transport_storage):
    rig = transport_storage
    book = StateBook(rackA=RackState("P1", "A"), rackB=RackState("P2", "A"))
    service = service_with(rig, book)
    left, right = slot("rackA", "A", "1"), slot("rackB", "A", "1")
    async with rig.factory() as session:
        original = await service.exchange_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            exchange_pairs=[BinExchangePair("b-1", left, "b-2", right)],
        )
        swapped = await service.exchange_bins(
            session,
            client_request_id=CLIENT,
            caller=caller(rig),
            exchange_pairs=[BinExchangePair("b-2", right, "b-1", left)],
        )
        assert swapped == original
        with pytest.raises(IdempotencyConflict):
            await service.move_bins(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                moves=[BinMove("b-1", left, HandoffPosition("out"))],
            )
        await session.commit()
        assert await counts(session, rig) == (1, 1)


async def test_rotate_requires_an_explicit_template(transport_storage):
    rig = transport_storage
    service = service_with(rig, StateBook(rack=RackState("KT16", "0")))
    async with rig.factory() as session:
        with pytest.raises(TypeError):
            await service.rotate_rack(
                session,
                client_request_id=CLIENT,
                caller=caller(rig),
                rack_id="rack",
                position=Rack("rack"),
                target_face="90",
            )
        assert await counts(session, rig) == (0, 0)
