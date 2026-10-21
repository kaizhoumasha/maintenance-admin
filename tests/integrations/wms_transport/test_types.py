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
