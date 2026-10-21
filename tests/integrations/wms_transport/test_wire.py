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


# ---- rotate_rack / move_bins / exchange_bins(合同 §3.2、§3.3、§4.1)----
from app.integrations.wms_transport import (  # noqa: E402
    BinExchangePair,
    BinMove,
    HandoffPosition,
    RackBinSlot,
)
from app.integrations.wms_transport.wire import (  # noqa: E402
    bin_move_data,
    exchange_bins_data,
    rotate_rack_data,
)


def slot(rack="rackA", face="A", number="1"):
    return RackBinSlot(rack, face, number)


def rotate(position=None, face="90", template="CTU02"):
    return rotate_rack_data("510004", position or RackPosition("KT16"), face, template)


def test_move_defaults_to_f01_but_rotate_has_no_default_template():
    # 换面模板随角色而异(五层 CTU02、转运/退料 CK04),不设默认,漏传即报错。
    assert move_rack_data("rack", Rack("rack"), RackPosition("KT16"))["rcs_template_id"] == "F01"
    with pytest.raises(TypeError):
        rotate_rack_data("rack", Rack("rack"), "90")


def test_rack_rotate_matches_contract_frozen_bytes():
    assert freeze(rotate()) == (
        b'{"operation_id":"01a13b00-cabd-7390-890a-0c093e7c07a1",'
        b'"operation":"transport.task.submit@v1","timestamp":1791991270077,'
        b'"data":{"transport_task_id":"transport-9af2751b-4d01-4203-88a7-649bbf69e810",'
        b'"kind":"RACK_ROTATE","rack_id":"510004","rcs_template_id":"CTU02",'
        b'"source":{"kind":"RACK_POSITION","location_code":"KT16"},'
        b'"target":{"kind":"RACK_POSITION","location_code":"KT16"},"target_face":"90"}}'
    )


def test_rack_rotate_writes_the_exact_position_to_both_source_and_target():
    # 合同 §3.2:位置原样同时写入 source 与 target;当前位置/面由 WMS 自行校验,WES 不推断。
    data = rotate(RackPosition("KT16"))
    assert data["source"] == data["target"] == {"kind": "RACK_POSITION", "location_code": "KT16"}


@pytest.mark.parametrize(
    "build",
    [
        lambda: rotate_rack_data("510004", Zone("510004"), "90", "CTU02"),
        lambda: rotate_rack_data("510004", Rack("510004"), "90", "CTU02"),
        lambda: rotate_rack_data("510004", Rack("another"), "90", "CTU02"),
        lambda: rotate_rack_data("510004", "KT16", "90", "CTU02"),
        lambda: rotate_rack_data("510004", RackPosition("KT16"), None, "CTU02"),
        lambda: rotate_rack_data("510004", RackPosition("KT16"), "", "CTU02"),
        lambda: rotate_rack_data("510004", RackPosition("KT16"), "x" * 11, "CTU02"),
        lambda: rotate_rack_data("510004", RackPosition("KT16"), "90", "CTU09"),
        lambda: rotate_rack_data("", RackPosition("KT16"), "90", "CTU02"),
    ],
)
def test_rack_rotate_rejects_contract_violations(build):
    with pytest.raises(InvalidTransportRequest):
        build()


def test_bin_move_matches_contract_shape_sorted_by_container_id():
    data = bin_move_data(
        [
            BinMove("b-2", HandoffPosition("in"), slot(number="2")),
            BinMove("b-1", slot(number="1"), HandoffPosition("out")),
        ]
    )
    payload = json.loads(freeze(data))["data"]
    assert list(payload) == ["transport_task_id", "kind", "moves"]
    assert payload["kind"] == "BIN_MOVE"
    assert [move["container_id"] for move in payload["moves"]] == ["b-1", "b-2"]
    assert payload["moves"][0] == {
        "container_id": "b-1",
        "source": {"kind": "RACK_BIN_SLOT", "rack_id": "rackA", "rack_face": "A", "slot_id": "1"},
        "target": {"kind": "HANDOFF_POSITION", "location_code": "out"},
    }


def test_bin_move_allows_a_shared_handoff_position():
    data = bin_move_data(
        [
            BinMove("b-1", slot(number="1"), HandoffPosition("out")),
            BinMove("b-2", slot(number="2"), HandoffPosition("out")),
        ]
    )
    assert len(data["moves"]) == 2


def moves(count):
    return [BinMove(f"b-{n}", slot(number=str(n)), HandoffPosition("out")) for n in range(count)]


@pytest.mark.parametrize(
    "build",
    [
        lambda: bin_move_data([]),
        lambda: bin_move_data(moves(5)),
        lambda: bin_move_data(moves(1) + moves(1)),
        lambda: bin_move_data([BinMove("b", HandoffPosition("in"), HandoffPosition("out"))]),
        lambda: bin_move_data([BinMove("b", slot(), slot())]),
        lambda: bin_move_data(
            [
                BinMove("b-1", slot(number="1"), slot(number="2")),
                BinMove("b-2", slot(number="3"), slot(number="2")),
            ]
        ),
        lambda: bin_move_data(
            [
                BinMove("b-1", slot(number="1"), HandoffPosition("o")),
                BinMove("b-2", slot(number="1"), HandoffPosition("o")),
            ]
        ),
        lambda: bin_move_data(
            [
                BinMove("b-1", slot(face="A"), HandoffPosition("o")),
                BinMove("b-2", slot(face="B", number="2"), HandoffPosition("o")),
            ]
        ),
        lambda: bin_move_data("not-a-list"),
        lambda: bin_move_data([("b", slot(), HandoffPosition("o"))]),
    ],
)
def test_bin_move_rejects_contract_violations(build):
    with pytest.raises(InvalidTransportRequest):
        build()


def test_bin_move_allows_different_faces_on_different_racks():
    data = bin_move_data(
        [
            BinMove("b-1", slot("rackA", "A", "1"), HandoffPosition("o")),
            BinMove("b-2", slot("rackB", "B", "1"), HandoffPosition("o")),
        ]
    )
    assert len(data["moves"]) == 2


def pair(left, right, left_bin="b-1", right_bin="b-2"):
    return BinExchangePair(left_bin, left, right_bin, right)


def test_exchange_expands_each_pair_into_two_reverse_members_sorted_by_container():
    data = exchange_bins_data([pair(slot("rackB", "A", "1"), slot("rackA", "A", "1"))])
    assert data["kind"] == "BIN_EXCHANGE"
    by_bin = {move["container_id"]: move for move in data["moves"]}
    assert [move["container_id"] for move in data["moves"]] == ["b-1", "b-2"]
    assert by_bin["b-1"]["source"] == by_bin["b-2"]["target"]
    assert by_bin["b-1"]["target"] == by_bin["b-2"]["source"]


def test_exchange_pair_orientation_does_not_change_the_request():
    left, right = slot("rackA", "A", "1"), slot("rackB", "A", "1")
    assert exchange_bins_data([pair(left, right)]) == exchange_bins_data(
        [pair(right, left, "b-2", "b-1")]
    )


def test_exchange_allows_one_group_or_two_crossing_groups():
    within = exchange_bins_data([pair(slot(number="1"), slot(number="2"))])
    assert len(within["moves"]) == 2
    crossing = exchange_bins_data(
        [
            pair(slot("rackA", "A", "1"), slot("rackB", "A", "1")),
            pair(slot("rackA", "A", "2"), slot("rackB", "A", "2"), "b-3", "b-4"),
        ]
    )
    assert len(crossing["moves"]) == 4


@pytest.mark.parametrize(
    "build",
    [
        lambda: exchange_bins_data([]),
        lambda: exchange_bins_data(
            [
                pair(slot(number="1"), slot(number="2")),
                pair(slot(number="3"), slot(number="4"), "b-3", "b-4"),
                pair(slot(number="5"), slot(number="6"), "b-5", "b-6"),
            ]
        ),
        lambda: exchange_bins_data([pair(slot(number="1"), slot(number="1"))]),
        lambda: exchange_bins_data([pair(slot(number="1"), slot(number="2"), "b", "b")]),
        lambda: exchange_bins_data(
            [
                pair(slot(number="1"), slot(number="2")),
                pair(slot(number="1"), slot(number="3"), "b-3", "b-4"),
            ]
        ),
        lambda: exchange_bins_data(
            [
                pair(slot(number="1"), slot(number="2")),
                pair(slot(number="3"), slot(number="4"), "b-1", "b-4"),
            ]
        ),
        lambda: exchange_bins_data(
            [
                pair(slot("rackA", "A", "1"), slot("rackA", "B", "2")),
            ]
        ),
        lambda: exchange_bins_data(
            [
                pair(slot("rackA", "A", "1"), slot("rackB", "A", "1")),
                pair(slot("rackC", "A", "1"), slot("rackB", "A", "2"), "b-3", "b-4"),
            ]
        ),
        lambda: exchange_bins_data(
            [
                pair(slot("rackA", "A", "1"), slot("rackA", "A", "2")),
                pair(slot("rackB", "A", "1"), slot("rackB", "A", "2"), "b-3", "b-4"),
            ]
        ),
        lambda: exchange_bins_data([pair(HandoffPosition("in"), slot(number="2"))]),
        lambda: exchange_bins_data("not-a-list"),
    ],
)
def test_exchange_rejects_contract_violations(build):
    with pytest.raises(InvalidTransportRequest):
        build()


MOVE_TEMPLATES = ["CTU01", "CTU03", "F01"]
ROTATE_TEMPLATES = ["CTU02", "CK04"]
NO_TEMPLATE = ["CK05", "ck04", "CK04 ", "", None, 4, []]


@pytest.mark.parametrize("template", MOVE_TEMPLATES)
def test_rack_move_accepts_only_its_own_templates(template):
    data = move_rack_data("rack", Rack("rack"), RackPosition("KT16"), None, template)
    assert data["rcs_template_id"] == template


@pytest.mark.parametrize("template", ROTATE_TEMPLATES + NO_TEMPLATE)
def test_rack_move_rejects_rotate_and_unknown_templates(template):
    with pytest.raises(InvalidTransportRequest):
        move_rack_data("rack", Rack("rack"), RackPosition("KT16"), None, template)


@pytest.mark.parametrize("template", ROTATE_TEMPLATES)
def test_rack_rotate_accepts_only_its_own_templates(template):
    assert rotate(template=template)["rcs_template_id"] == template


@pytest.mark.parametrize("template", MOVE_TEMPLATES + NO_TEMPLATE)
def test_rack_rotate_rejects_move_and_unknown_templates(template):
    with pytest.raises(InvalidTransportRequest):
        rotate(template=template)
