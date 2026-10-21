import json
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.wms_transport import InvalidTransportRequest, Rack, RackPosition, Zone
from app.integrations.wms_transport.wire import (
    freeze_request,
    move_rack_data,
    timestamp_ms,
)

OPERATION_ID = "01a13b00-cabd-7390-890a-0c093e7c07a1"
TASK_ID = "transport-9af2751b-4d01-4203-88a7-649bbf69e810"


def freeze(data):
    return freeze_request(OPERATION_ID, TASK_ID, 1791991270077, data)


def test_rack_move_matches_contract_frozen_bytes():
    data = move_rack_data("510004", Rack("510004"), RackPosition("KT16"), "90", "CTU01")
    assert freeze(data) == (
        b'{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1",'
        b'"operation":"transport.task.submit@v1","timestamp":1791991270077,'
        b'"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810",'
        b'"kind":"RACK_MOVE","rack_id":"510004","rcs_template_id":"CTU01",'
        b'"source":{"kind":"RACK","location_code":"510004"},'
        b'"target":{"kind":"RACK_POSITION","location_code":"KT16"},"target_face":"90"}}'
    )


def test_default_template_and_unspecified_face_have_one_wire_form():
    implicit = move_rack_data("rack", Zone("source"), Rack("rack"))
    explicit = move_rack_data("rack", Zone("source"), Rack("rack"), None, "F01")
    assert freeze(implicit) == freeze(explicit)
    payload = json.loads(freeze(implicit))["data"]
    assert payload["rcs_template_id"] == "F01"
    assert "target_face" not in payload


def test_unicode_and_opaque_face_are_preserved_exactly():
    face = " e\u0301🧭 "
    payload = json.loads(freeze(move_rack_data("架", Rack("架"), Zone("区域"), face)))
    assert payload["data"]["target_face"] == face
    assert payload["data"]["rack_id"] == "架"
    assert "区域".encode() in freeze(move_rack_data("架", Rack("架"), Zone("区域")))


def test_millisecond_clock_is_exact_and_timezone_aware():
    offset = timezone(timedelta(hours=8))
    assert timestamp_ms(datetime(1970, 1, 1, 8, 0, 0, 123999, tzinfo=offset)) == 123
    with pytest.raises(InvalidTransportRequest):
        timestamp_ms(datetime(2026, 10, 22))


def test_fixed_body_limit_uses_final_utf8_bytes():
    data = move_rack_data("rack", RackPosition("x"), Zone("target"))
    padding = 262144 - len(freeze(data))
    boundary = move_rack_data("rack", RackPosition("x" * (padding + 1)), Zone("target"))
    assert len(freeze(boundary)) == 262144
    for code in ("x" * (padding + 2), "架" * (padding + 1)):
        with pytest.raises(InvalidTransportRequest):
            freeze(move_rack_data("rack", RackPosition(code), Zone("target")))
