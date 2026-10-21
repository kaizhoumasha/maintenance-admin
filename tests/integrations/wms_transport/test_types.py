from dataclasses import FrozenInstanceError

import pytest

from app.integrations.wms_transport import (
    InvalidTransportRequest,
    Rack,
    RackPosition,
    TransportCaller,
    Zone,
)


@pytest.mark.parametrize("location", [Rack, Zone, RackPosition])
def test_location_codes_are_immutable_utf8_text(location):
    value = location(" 架 e\u0301 ")
    assert value.code == " 架 e\u0301 "
    with pytest.raises(FrozenInstanceError):
        value.code = "changed"
    for invalid in ("", "bad\x00", "\ud800", None, 123):
        with pytest.raises(InvalidTransportRequest):
            location(invalid)


def test_caller_requires_valid_routing_text():
    for workline, station in (("", None), ("line\x00", None), ("line", ""), ("line", "\ud800")):
        with pytest.raises(InvalidTransportRequest):
            TransportCaller(workline, station)


def test_bin_locations_and_rack_state_are_valid_immutable_text():
    from app.integrations.wms_transport import HandoffPosition, RackBinSlot, RackState

    slot = RackBinSlot("rack", "A", "s1")
    assert (slot.rack_id, slot.rack_face, slot.slot_id) == ("rack", "A", "s1")
    with pytest.raises(FrozenInstanceError):
        slot.slot_id = "changed"
    assert HandoffPosition("in").code == "in" and RackState("KT16", "0").face == "0"
    for build in (
        lambda: RackBinSlot("", "A", "s"),
        lambda: RackBinSlot("rack", "x" * 11, "s"),
        lambda: RackBinSlot("rack", "A", "s\x00"),
        lambda: RackBinSlot("rack", None, "s"),
        lambda: HandoffPosition(""),
        lambda: RackState("", "0"),
        lambda: RackState("KT16", ""),
        lambda: RackState("KT16", "🧭" * 11),
    ):
        with pytest.raises(InvalidTransportRequest):
            build()


def test_bin_move_and_exchange_pair_require_their_own_shapes():
    from app.integrations.wms_transport import (
        BinExchangePair,
        BinMove,
        HandoffPosition,
        RackBinSlot,
    )

    slot = RackBinSlot("rack", "A", "s1")
    assert BinMove("bin", HandoffPosition("in"), slot).bin_code == "bin"
    assert BinExchangePair("a", slot, "b", RackBinSlot("rack", "A", "s2")).right_bin_code == "b"
    for build in (
        lambda: BinMove("", HandoffPosition("in"), slot),
        lambda: BinMove("bin", "in", slot),
        lambda: BinMove("bin", Rack("x"), slot),
        lambda: BinExchangePair("a", slot, "", slot),
        lambda: BinExchangePair("a", HandoffPosition("in"), "b", slot),
    ):
        with pytest.raises(InvalidTransportRequest):
            build()
